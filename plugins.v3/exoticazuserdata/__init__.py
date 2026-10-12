# -*- coding: utf-8 -*-
"""
ExoticaZ 用户数据适配插件（MoviePilot V3 插件源标准结构）

机制: 通过官方插件钩子 get_module() 劫持系统模块方法 refresh_userdata
（契约: aggregation=FIRST_NON_EMPTY, public_to_plugins=True, plugin_short_circuit=True,
插件 provider 先于宿主模块执行）。仅接管 domain == exoticaz.to，其余站点返回 None
交回宿主内置 IndexerModule 原生逻辑。入库/事件/消息/调度/API 全部复用宿主链路。

V3 插件源规范(与官方 MoviePilot-Plugins 仓库一致):
  - 类名 = 插件 ID（生命周期以 plugin.__name__ 作为插件 ID）
  - 安装目录 = plugins.v3/<id 小写>/，由市场清单 package.v3.json 索引
  - 基类从 app.plugins 导入（官方惯例；app.sdk.plugin.base 为 canonical 路径）

设计说明: ../DESIGN.md
解析逻辑: ./userdata_parser.py（纯逻辑, 样本驱动, 可独立测试）
"""
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import site_patch
from .site_bin_maintenance import BinMaintainer, DEFAULT_KEY
from .userdata_parser import (
    EXOTICAZ_DOMAIN_SUFFIX,
    RequestThrottle,
    base_url_of,
    collect_user_data,
    is_exoticaz,
)

try:  # 官方惯例导入路径；canonical 为 app.sdk，宿主外(纯逻辑测试)再降级
    from app.plugins import _PluginBase
except ImportError:
    try:
        from app.sdk.plugin.base import _PluginBase
    except ImportError:
        _PluginBase = object

try:
    from app.adapters.network.http import RequestUtils
except ImportError:  # 宿主外(纯逻辑测试)降级
    RequestUtils = None

try:
    from app.schemas.site import SiteUserData
except ImportError:  # pragma: no cover
    SiteUserData = None


SEEDING_CACHE_TTL_HOURS = 12


class ExoticaZUserData(_PluginBase):
    # 插件 ID 即类名（V3 生命周期以 plugin.__name__ 为 ID），勿改名
    plugin_name = "ExoticaZ 用户数据"
    plugin_desc = "exoticaz.to 站点用户面板数据适配：接管 refresh_userdata，解析 ratio-bar/个人页/做种列表；做种体积 12h 缓存以控制站点请求量。"
    plugin_version = "1.4.30"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/refs/heads/main/icons/spider.png"
    plugin_author = "iovejieba"
    author_url = "https://github.com/jxxghp/MoviePilot"
    plugin_order = 20
    auth_level = 1

    _enable = False
    _bin_maintain = True
    _fernet_key = ""
    _crawl_in_progress = False
    _sukebei_patch = False
    _collect_running = False

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._enable = bool(config and config.get("enabled"))
        self._bin_maintain = bool(config.get("bin_maintain", True)) if config else True
        self._fernet_key = str((config or {}).get("fernet_key") or "")
        self._sukebei_patch = bool((config or {}).get("sukebei_patch", False))
        if self._enable:
            # 生命周期标记管理必须在同步段(写入/确认时点无竞态)
            self._update_patch_lifecycle(new_process=True)
        if self._enable and self._bin_maintain:
            # 启动自检走后台线程, 不阻塞宿主启动；失败只记日志
            threading.Thread(target=self._safe_maintain, daemon=True).start()
        if self._enable:
            # 域名白名单补丁生命周期自检(仅插件启用时):
            #   开关开 + 补丁不在 → 自愈重打(MP 升级冲掉/历史清失)
            #   开关关 + 补丁还在 → 回滚清理
            threading.Thread(target=self._sukebei_lifecycle_check, daemon=True).start()

    def _mp_root(self) -> Optional[Path]:
        """定位 MP 主项目根目录。

        app/domain/site.py 的 __file__ = <root>/app/domain/site.py,
        故 root = parents[2](容器内即 /app)。"""
        try:
            from app.domain import site as site_mod
            return Path(site_mod.__file__).resolve().parents[2]
        except Exception:
            return None

    def _update_patch_lifecycle(self, new_process: bool = False) -> None:
        """补丁生命周期标记管理(同步段执行, 无竞态)。

        持久化标记 patch_pending:
          True  = 补丁有写入动作且尚未被任何进程启动确认 → 状态卡显示中间态
          False/缺 = 无待确认写入 → 状态卡显示终态(绿/灰)

        逻辑:
          - 开关开 + 磁盘无补丁 → 后台对齐 + 打 pending 标记(写入后待重启)
          - 开关开 + 磁盘有补丁 + new_process(本次是进程启动) + 标记挂着
            → 运行模块已在本进程加载补丁 → 确认生效, 清标记 → 绿
          - 开关开 + 磁盘有补丁 + 标记无 → 稳定已生效态(绿)
          - 开关关 + 磁盘有补丁 → 后台回滚 + 打 pending 标记(回滚后待重启)
          - 开关关 + 磁盘无补丁 + new_process + 标记挂着 → 确认回滚完成, 清标记 → 灰
        """
        import logging
        try:
            from app.sdk.logging import logger
        except ImportError:
            logger = logging.getLogger("exoticazuserdata")
        try:
            root = self._mp_root()
            if root is None:
                return
            site_file = root / site_patch.RELATIVE_FILE
            on_disk = site_patch.domain_in_constant(site_file.read_text(encoding="utf-8"))
            mark = self._load_seeding_cache() or {}
            pending = bool(mark.get("patch_pending"))
            desired = self._sukebei_patch

            # --- 意图与磁盘不一致 → 触发对齐 + 打标记 ---
            if desired and not on_disk:
                threading.Thread(target=self._sukebei_lifecycle_check, daemon=True).start()
                mark["patch_pending"] = True
                mark["patch_desired"] = True
                self._save_seeding_cache(mark)
                return
            if not desired and on_disk:
                threading.Thread(target=self._sukebei_lifecycle_check, daemon=True).start()
                mark["patch_pending"] = True
                mark["patch_desired"] = False
                self._save_seeding_cache(mark)
                return

            # --- 意图与磁盘一致 ---
            if desired and on_disk:
                if new_process and pending:
                    # 本次进程启动 = 补丁已被本进程加载 = 生效确认, 清标记 + 转正
                    mark.pop("patch_pending", None)
                    mark["patch_desired"] = True
                    mark["patch_effective"] = True
                    self._save_seeding_cache(mark)
                    return
                mark["patch_desired"] = True
                mark["patch_effective"] = True
                self._save_seeding_cache(mark)
                return
            # not desired and not on_disk
            if new_process and pending:
                # 本次进程启动确认了回滚完成, 清标记
                mark.pop("patch_pending", None)
                mark["patch_desired"] = False
                mark["patch_effective"] = False
                self._save_seeding_cache(mark)
                return
            mark["patch_desired"] = False
            mark["patch_effective"] = False
            self._save_seeding_cache(mark)
        except Exception as e:  # noqa: BLE001
            logger.error(f"ExoticaZUserData: 补丁生命周期标记异常: {e}")

    def _sukebei_lifecycle_check(self) -> None:
        """补丁生命周期自检: 按开关意图对齐磁盘状态(单向修复, 失败保真)。

        开关开 + 磁盘无补丁 → 重打(用户要功能; MP 升级冲掉/历史清失都算丢失)
        开关关 + 磁盘有补丁 → 移除(用户不要功能; 历史残留/误写入都算多余)
        """
        import logging
        try:
            from app.sdk.logging import logger
        except ImportError:
            logger = logging.getLogger("exoticazuserdata")
        try:
            root = self._mp_root()
            if root is None:
                return
            on_disk = site_patch.domain_in_constant(
                (root / site_patch.RELATIVE_FILE).read_text(encoding="utf-8"))
            mark = self._load_seeding_cache() or {}
            # 对齐动作发生后必须刷新生命周期标记:
            #   自愈重打 → patch_effective=False(进程还没加载新补丁, 状态卡橙"等待重启")
            #   回滚移除 → patch_effective=False(进程还带着旧补丁, 状态卡橙"回滚后需重启")
            # 缺了这一步, 状态卡读到 effective=None → 按运行模块判 → 直接绿(2026-10-12 实机)
            if self._sukebei_patch and not on_disk:
                ok, msg = site_patch.apply_or_remove(root, remove=False)
                if ok:
                    mark["patch_desired"] = True
                    mark["patch_effective"] = False
                    self._save_seeding_cache(mark)
                level = logger.info if ok else logger.error
                level(f"ExoticaZUserData: 域名白名单补丁自愈{'成功' if ok else '失败'}: {msg}(重启 MP 生效)")
            elif not self._sukebei_patch and on_disk:
                ok, msg = site_patch.apply_or_remove(root, remove=True)
                if ok:
                    mark["patch_desired"] = False
                    mark["patch_effective"] = False
                    self._save_seeding_cache(mark)
                level = logger.info if ok else logger.error
                level(f"ExoticaZUserData: 域名白名单补丁回滚{'成功' if ok else '失败'}: {msg}(重启 MP 生效)")
        except Exception as e:  # noqa: BLE001
            logger.error(f"ExoticaZUserData: 补丁生命周期自检异常: {e}")

    def _sukebei_rollback_check(self) -> None:
        """兼容别名: 等价于生命周期自检。"""
        self._sukebei_lifecycle_check()

    def get_state(self) -> bool:
        return self._enable

    def get_module(self) -> Optional[Dict[str, Any]]:
        """劫持系统模块方法 refresh_userdata（契约级扩展点，见 DESIGN.md §3/§4）。"""
        if self.get_state():
            return {"refresh_userdata": self.refresh_userdata}
        return None

    # ---------------------------------------------------------------- bin 自维护

    def _safe_maintain(self) -> None:
        """bin 自维护入口（线程内执行），任何异常都不外抛。"""
        try:
            from app.sdk.logging import logger
        except ImportError:
            try:
                from app.runtime.log import logger
            except ImportError:
                import logging
                logger = logging.getLogger("exoticazuserdata")

        try:
            from app.application.site import sites as sites_module
            bin_path = Path(sites_module.__file__).parent / "user.sites.v3.bin"
            sites_file = Path(sites_module.__file__)
        except Exception as e:  # noqa: BLE001
            logger.warn(f"ExoticaZUserData: 无法定位站点模块/bin: {e}")
            return

        def fetch(url: str):
            try:
                if RequestUtils is None:
                    return None
                # 跟随宿主 GITHUB_PROXY 设置(官方 resource.py 同款前缀模式),
                # 直连 raw.githubusercontent 不通时走用户配置的加速前缀
                try:
                    from app.runtime.settings import get_runtime_setting
                    gh = str(get_runtime_setting('GITHUB_PROXY') or "").strip()
                except Exception:
                    gh = ""
                if gh and url.startswith("https://raw.githubusercontent.com/"):
                    url = (gh if gh.endswith("/") else gh + "/") + url
                resp = RequestUtils(timeout=20).get_res(url)
                if resp is None:
                    return None
                return resp.status_code, resp.content
            except Exception:
                return None

        key = self._fernet_key.encode() if self._fernet_key else DEFAULT_KEY
        state_file = self._state_file()
        maintainer = BinMaintainer(key=key, fetch=fetch, log=lambda m: logger.info(f"ExoticaZUserData: {m}"))
        result = maintainer.run(bin_path, key_source=lambda: state_file.write_text(
            (maintainer.key or b"").decode()), sites_module_file=sites_file)
        action = result.get("action", "skip")
        detail = result.get("detail", "")
        if action == "merged":
            logger.info(f"ExoticaZUserData: 站点资源已合并，重启 MP 后生效 ({detail})")
        elif action == "error":
            logger.error(f"ExoticaZUserData: 站点资源维护失败: {detail}")
        else:
            logger.info(f"ExoticaZUserData: 站点资源自检通过（{detail}）")
        # 维护历史持久化(详情页展示, 保留最近 20 条)
        try:
            history = self.get_data("maintain_history") or []
            import datetime as _dt
            history.insert(0, {"time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "action": action, "detail": detail,
                               "key": (maintainer.key or b"").decode()[:11] + "…"})
            self.save_data("maintain_history", history[:20])
        except Exception:
            pass

    def _state_file(self) -> Path:
        try:
            base = self.get_data_path("ExoticaZUserData")
        except Exception:
            import tempfile
            base = Path(tempfile.gettempdir())
        return base / "sites_key.txt"

    def maintain_job(self) -> None:
        """每日服务的执行入口。"""
        self._safe_maintain()

    # ---------------------------------------------------------------- 劫持实现

    def refresh_userdata(self, site: Optional[Dict[str, Any]] = None) -> Optional["SiteUserData"]:
        """仅接管 exoticaz；其余站点返回 None 交回宿主内置解析。"""
        site = site or {}
        url = str(site.get("url") or site.get("domain") or "")
        if not is_exoticaz(url):
            return None
        if SiteUserData is None:
            return None
        if self._collect_running:
            # 并发去重: 已有同一轮采集在执行, 用缓存兜底快速返回(不重复请求站点)
            cache = self._load_seeding_cache() or {}
            if cache.get("username"):
                return SiteUserData(
                    domain=urlsplit(url).netloc or EXOTICAZ_DOMAIN_SUFFIX,
                    name=str(site.get("name") or ""),
                    username=cache["username"], userid=cache["username"],
                    user_level=cache.get("user_level") or "",
                    join_at=cache.get("join_at") or "",
                    bonus=float(cache.get("bonus") or 0),
                    upload=int(cache.get("upload") or 0),
                    download=int(cache.get("download") or 0),
                    ratio=float(cache.get("ratio") or 0),
                    seeding=int(cache.get("seeding") or 0),
                    leeching=int(cache.get("leeching") or 0),
                    seeding_size=int(cache.get("seeding_size") or 0),
                    seeding_info=list(cache.get("seeding_info") or []),
                )
            return None
        self._collect_running = True
        try:
            return self._do_refresh_userdata(site, url)
        finally:
            self._collect_running = False

    def _do_refresh_userdata(self, site: Dict[str, Any], url: str) -> Optional["SiteUserData"]:
        domain = urlsplit(url).netloc or EXOTICAZ_DOMAIN_SUFFIX
        name = str(site.get("name") or domain)
        cache = self._load_seeding_cache() or {}
        fresh = self._seeding_cache_fresh(cache)
        if not fresh and not self._crawl_in_progress:
            # 做种页爬取(可能数十秒)放到后台线程, 绝不阻塞刷新调用
            self._crawl_in_progress = True
            threading.Thread(target=self._seeding_crawl_worker,
                             args=(dict(site),), daemon=True).start()
        data = collect_user_data(
            fetch=self._make_fetcher(site),
            base_url=base_url_of(url),
            include_active=False,    # 刷新路径永远快速: 只取 ratio-bar + 个人页
            profile_cache=cache,     # 个人页抓取失败时的精确值兜底(防幻影增量)
        )
        if not data.get("err_msg"):
            # 做种体积/明细来自缓存(后台线程自愈更新)
            if cache.get("seeding_size") is not None:
                data["seeding_size"] = int(cache.get("seeding_size") or 0)
                data["seeding_info"] = list(cache.get("seeding_info") or [])
        if data.get("err_msg") and not data.get("userid"):
            # 与宿主内置行为对齐: 带 err_msg 且无 userid -> 不持久化, 手动刷新可见错误
            return SiteUserData(domain=domain, name=name, err_msg=data["err_msg"])
        return SiteUserData(domain=domain, name=name, **{
            k: v for k, v in data.items() if k != "err_msg"
        })

    def _seeding_crawl_worker(self, site: Dict[str, Any]) -> None:
        """后台爬取做种页并更新缓存(自愈): 完成后下一次刷新即带真数。"""
        try:
            from app.sdk.logging import logger
        except ImportError:
            import logging
            logger = logging.getLogger("exoticazuserdata")
        try:
            url = str(site.get("url") or site.get("domain") or "")
            old_cache = self._load_seeding_cache() or {}
            data = collect_user_data(
                fetch=self._make_fetcher(site),
                base_url=base_url_of(url),
                include_active=True,
                profile_cache=old_cache,   # 个人页失败时沿用上次精确值
            )
            if data.get("username") and not data.get("err_msg"):
                self._save_seeding_cache({
                    "ts": time.time(),
                    "username": data.get("username") or "",
                    "upload": data.get("upload") or 0,
                    "download": data.get("download") or 0,
                    "ratio": data.get("ratio") or 0.0,
                    "join_at": data.get("join_at") or "",
                    "user_level": data.get("user_level") or "",
                    "seeding_size": data.get("seeding_size") or 0,
                    "seeding_info": data.get("seeding_info") or [],
                })
                logger.info(f"ExoticaZUserData: 做种体积缓存已更新 "
                            f"({data.get('seeding_size', 0)/(1<<30):.2f} GiB)")
            else:
                logger.warn("ExoticaZUserData: 后台做种页爬取未获有效数据")
        except Exception as e:  # noqa: BLE001
            logger.error(f"ExoticaZUserData: 后台做种页爬取异常: {e}")
        finally:
            self._crawl_in_progress = False

    def _load_seeding_cache(self) -> Optional[Dict[str, Any]]:
        try:
            return self.get_data("seeding_cache") or None
        except Exception:
            return None

    def _save_seeding_cache(self, cache: Dict[str, Any]) -> None:
        try:
            self.save_data("seeding_cache", cache)
        except Exception:
            pass

    @staticmethod
    def _seeding_cache_fresh(cache: Optional[Dict[str, Any]]) -> bool:
        if not cache or not cache.get("ts"):
            return False
        return (time.time() - float(cache["ts"])) < SEEDING_CACHE_TTL_HOURS * 3600

    def _make_fetcher(self, site: Dict[str, Any]):
        """用宿主 RequestUtils 构造带 Cookie/UA/代理的请求器 + 1 rps 节流。"""
        throttle = RequestThrottle()

        if RequestUtils is None:  # 离线测试环境
            return lambda url: None

        utils = RequestUtils(
            ua=str(site.get("ua") or ""),
            cookies=str(site.get("cookie") or ""),
            proxies=site.get("proxies") or site.get("proxy"),
            timeout=30,
            use_session=True,
        )

        def fetch(url: str):
            try:
                return utils.get_res(url)
            except Exception:
                return None

        return fetch

    # ---------------------------------------------------------------- 插件壳

    def get_form(self) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "bin_maintain",
                                              "label": "站点资源自维护"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "sukebei_patch",
                                              "label": "Sukebei 独立站点"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "fernet_key",
                                        "label": "Fernet Key(留空使用内建值)",
                                        "placeholder": "仅在日志提示 key 轮换解密失败时填写",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "text": "Sukebei 开关会修改 MP 主项目文件 app/domain/site.py 一行"
                                                "(向官方域名白名单机制追加 sukebei.nyaa.si, 与 u2.dmhy.org 先例同构)。"
                                                "开启后需重启 MP 生效；关闭并重启即自动回滚。补丁带多锚点校验+AST 结构"
                                                "等价检查+自动备份, 失败保真不会损坏主项目；MP 升级冲掉补丁后本插件会自动重打。",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": "本插件做两件事：1.接管 ExoticaZ(exoticaz.to) 的站点用户面板数据"
                                                "(等级/上传下载/分享率/魔力/做种数)；2.把 ExoticaZ 的搜索适配自动"
                                                "合并进官方 user.sites.v3.bin——官方资源更新后 24 小时内自动重合并，"
                                                "版本始终领先官方一位防覆盖，合并后需重启一次 MP 生效。",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "text": "使用前提：已在站点管理中添加 exoticaz.to 并配置有效 Cookie。"
                                                "本地安装器安装后首次显示加载失败属正常(实例默认停用)，"
                                                "打开卡片启用开关即可。",
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], {"enabled": True, "bin_maintain": True, "fernet_key": "", "sukebei_patch": False}

    def _sukebei_status_card(self) -> Dict[str, Any]:
        """域名白名单补丁状态卡: 按用户当前意图(开关)判定, 全态如实显示。"""
        root = self._mp_root()

        if not self._sukebei_patch:
            # ---- 用户意图: 关闭 ----
            state = site_patch.patch_state(root, enabled=False) if root else "off"
            meta = {
                "off": ("info", "域名白名单补丁：未启用。",
                        "如需将 Sukebei 作为独立站点使用，请在设置中开启对应开关并保存。"),
                "disabled_pending": ("warning", "Sukebei 支持：已关闭，重启后彻底停用。",
                                     "补丁已从主项目移除，当前进程仍在使用旧代码——重启 MoviePilot 后彻底关闭。"),
                "disabled_pending_restart": ("warning", "Sukebei 支持：已关闭，等待回滚。",
                                             "补丁回滚已排队，重启 MoviePilot 后主项目恢复原状。"),
            }.get(state, ("info", "域名白名单补丁：未启用。",
                          "如需将 Sukebei 作为独立站点使用，请在设置中开启对应开关并保存。"))
            vtype, title, sub = meta
            return {
                "component": "VAlert",
                "props": {"type": vtype, "variant": "tonal", "text": f"{title} {sub}"},
            }

        # ---- 用户意图: 开启 ----
        # 生命周期标记 effective: 只有"上次保存时已确认生效"才允许绿色,
        # 刚写入(待重启)时为 False → 状态卡如实显示中间态
        mark = self._load_seeding_cache() or {}
        effective = mark.get("patch_effective")
        state = site_patch.patch_state(root, enabled=True, effective=effective) if root else "error"
        meta = {
            "applied": ("success", "域名白名单补丁：已应用并生效。",
                        "Sukebei(sukebei.nyaa.si) 可作为独立站点添加与使用。"),
            "pending_restart": ("warning", "域名白名单补丁：已写入，等待重启生效。",
                                "补丁已应用到主项目，重启 MoviePilot 后 Sukebei 即可作为独立站点添加。"),
            "error": ("warning", "域名白名单补丁：准备中，稍后自动完成。",
                      "补丁将在后台自动应用(也可尝试保存设置触发)，应用后需重启 MP。"),
        }.get(state, ("warning", "域名白名单补丁：准备中。",
                      "稍后自动完成，如长时间不变请查看插件日志。"))
        vtype, title, sub = meta
        return {
            "component": "VAlert",
            "props": {"type": vtype, "variant": "tonal", "text": f"{title} {sub}"},
        }

    def get_page(self) -> Optional[List[Dict[str, Any]]]:
        """详情页：运行状态卡 + 站点资源维护历史 + H&R 速查。"""
        try:
            history = self.get_data("maintain_history") or []
        except Exception:
            history = []
        action_map = {"merged": "已合并", "skip": "自检通过", "error": "失败"}
        ths = [{"component": "th", "text": h} for h in ["时间", "动作", "详情", "Key"]]
        rows = []
        for h in history:
            cells = [
                h.get("time") or "-",
                action_map.get(h.get("action"), h.get("action") or "-"),
                h.get("detail") or "-",
                h.get("key") or "-",
            ]
            rows.append({
                "component": "tr",
                "content": [{"component": "td", "text": c} for c in cells],
            })
        if not rows:
            rows = [{
                "component": "tr",
                "content": [{"component": "td",
                             "text": "暂无维护记录，启用插件并打开站点资源自维护后自动产生…"}],
            }]
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": "success" if self._enable else "info",
                                "variant": "tonal",
                                "text": ("用户面板劫持：已激活。" if self._enable
                                         else "用户面板劫持：未启用。")
                                        + "启用后 ExoticaZ 的站点数据刷新由本插件解析"
                                          "(ratio-bar/个人页/做种列表三段式)。",
                            },
                        }],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": "success" if self._bin_maintain else "info",
                                "variant": "tonal",
                                "text": ("站点资源自维护：运行中。" if self._bin_maintain
                                         else "站点资源自维护：已关闭。")
                                        + "启动自检 + 每 24 小时自动同步官方资源并重合并 ExoticaZ 适配。",
                            },
                        }],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [self._sukebei_status_card()],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VTable",
                            "props": {"density": "comfortable", "hover": True},
                            "content": [
                                {"component": "thead",
                                 "content": [{"component": "tr", "content": ths}]},
                                {"component": "tbody", "content": rows},
                            ],
                        }],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": "info",
                                "variant": "tonal",
                                "text": "H&R 规则速查：要求时长=ceil(72+2x体积GiB)小时(50GB 以上为对数段)；"
                                        "须在下载完成后 96 小时内达成，到期未达标即记账(做种中也不例外)；"
                                        "继续做种到倒计时归零自动解除；BP 清除=10 BP/小时。"
                                        "完整规则合同见 exoticaz-python-reference 文档。",
                            },
                        }],
                    },
                ],
            },
        ]

    def get_api(self) -> Optional[List[Dict[str, Any]]]:
        return None

    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        """每日自维护服务：官方资源更新后自动重合并 overlay。"""
        if not self.get_state():
            return None
        return [{
            "id": "ExoticaZUserDataBinMaintain",
            "name": "ExoticaZ 站点资源自维护",
            "trigger": "interval",
            "func": self.maintain_job,
            "kwargs": {"hours": 24},
        }]

    def stop_service(self) -> None:
        pass
