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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

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


class ExoticaZUserData(_PluginBase):
    # 插件 ID 即类名（V3 生命周期以 plugin.__name__ 为 ID），勿改名
    plugin_name = "ExoticaZ 用户数据"
    plugin_desc = "exoticaz.to 站点用户面板数据适配：接管 refresh_userdata，解析 ratio-bar / 个人页 / 做种列表。"
    plugin_version = "1.3.5"
    plugin_icon = "https://exoticaz.to/favicon.ico"
    plugin_author = "HaoLekk"
    author_url = "https://github.com/jxxghp/MoviePilot"
    plugin_order = 20
    auth_level = 1

    _enable = False
    _bin_maintain = True
    _fernet_key = ""

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._enable = bool(config and config.get("enabled"))
        self._bin_maintain = bool(config.get("bin_maintain", True)) if config else True
        self._fernet_key = str((config or {}).get("fernet_key") or "")
        if self._enable and self._bin_maintain:
            # 启动自检走后台线程, 不阻塞宿主启动；失败只记日志
            threading.Thread(target=self._safe_maintain, daemon=True).start()

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
                resp = RequestUtils(timeout=30).get_res(url)
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

        domain = urlsplit(url).netloc or EXOTICAZ_DOMAIN_SUFFIX
        name = str(site.get("name") or domain)
        data = collect_user_data(
            fetch=self._make_fetcher(site),
            base_url=base_url_of(url),
        )
        if data.get("err_msg") and not data.get("userid"):
            # 与宿主内置行为对齐: 带 err_msg 且无 userid -> 不持久化, 手动刷新可见错误
            return SiteUserData(domain=domain, name=name, err_msg=data["err_msg"])
        return SiteUserData(domain=domain, name=name, **{
            k: v for k, v in data.items() if k != "err_msg"
        })

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
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "bin_maintain",
                                              "label": "站点资源自维护(自动合并进官方bin)"},
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
        ], {"enabled": True, "bin_maintain": True, "fernet_key": ""}

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
