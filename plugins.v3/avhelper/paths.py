"""运行期路径与外部依赖解析：数据根（SQLite/缓存）与 javdb-cli 二进制。

别人把这套插件装进自己的 MoviePilot 时，容器里既没有本机那个 `/avhelper` 挂载卷，
也没有预置好的 `javdb` 二进制，所以两条路径都不能写死。解析顺序固定为
「插件配置 → 历史固定路径（老部署原地不动）→ 通用回落」：

- **数据根**：配置 `data_root` → 存在 `/avhelper` 卷时用 `/avhelper/cache` →
  MoviePilot 的插件数据目录 `<PLUGIN_DATA_PATH>/AVHelper`（默认 `/config/plugins/AVHelper`，
  其下 `meta/` 放 SQLite 与 JSON 缓存、`images/` 放图片缓存、`bin/` 放 javdb）。
  SQLite 台账、JSON 缓存、图片缓存同根，容量淘汰也按这个根扫描。
- **javdb 二进制**：配置 `javdb_bin` → `/avhelper/bin/javdb` → `PATH` 上的 `javdb` →
  数据根下的 `bin/javdb`（缺文件时由 `ensure_javdb()` 自动从官方 Release 下载）。

解析结果由 `configure()` 在 `init_plugin()` 里写一次，其余模块只通过取值函数读取，
不各自缓存路径副本（否则改配置后要重启容器才生效）。
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional, Tuple

# 缓存容量预算（GB）：**默认 1G**（2026-10-14 用户要求，从写死的 60G 改成可配置）。
# 依据是实测：线上数据根长期占用 180 MB 左右（图片 + JSON 缓存 + SQLite 一起），
# 60G 的旧上限对 NAS 用户无所谓，对小硬盘用户却是一句「随时可能吃掉 60G」的恐吓。
# 留 0.1G 下限是硬保护：上限归零会让每晚的淘汰把缓存目录整个清空。
DEFAULT_CACHE_MAX_GB = 1.0
CACHE_MAX_GB_MIN = 0.1
CACHE_MAX_GB_MAX = 4096.0

# 老部署的固定坐标：数据卷挂到容器 `/avhelper`，插件数据与 javdb 二进制共用这个前缀
LEGACY_ROOT = Path("/avhelper")
LEGACY_DATA_ROOT = LEGACY_ROOT / "cache"
LEGACY_JAVDB = LEGACY_ROOT / "bin" / "javdb"

# javdb-cli 上游（Go 单文件二进制，官方 Release 提供各平台压缩包）
JAVDB_REPO = "FlanChanXwO/javdb-cli"
# **推荐版本**（新装/低于下限时下载它）：本插件在这版上实测通过。
# 0.8.2 → 0.8.4（2026-11-20，CASE-20261120-01）：JavDB 服务端收紧了 rankings/search 的
# 认证（匿名请求被拒，`JWTVerificationError: Invalid Signature`），0.8.4 修复「已登录
# 也不带 token」的缺陷（上游 PR #107）。
JAVDB_VERSION = "0.8.4"
# **下限版本**（2026-11-20 用户定的策略修订）：低于它的已装二进制**必须**自动升级——
# 0.8.2 实测已被服务端拒绝，卡在上面等于功能坏死。高于下限的其它版本（用户通过
# 「检测更新」自选装的新版）一律不动，只在日志/界面提示与本插件实测版本不同。
# 纯锁定策略这次已经付出代价（服务端一变，用户卡死在旧版等作者 bump），不再用。
JAVDB_MIN_VERSION = "0.8.4"
# Go 的 GOOS/GOARCH 命名口径（平台的其它写法在 _platform_tag() 里归一）
_SYSTEMS = {"linux": "linux", "darwin": "darwin", "windows": "windows"}
_MACHINES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

_DATA_ROOT: Optional[Path] = None
_JAVDB_BIN: Optional[str] = None


def _fallback_root() -> Path:
    """拿不到宿主插件数据目录时的兜底数据根（历史坐标，保持与老部署一致）。"""
    return LEGACY_DATA_ROOT


def _resolve_root(configured: str, fallback: Optional[Path]) -> Path:
    """按「配置 → 历史卷 → 宿主插件数据目录」的顺序定出数据根。"""
    text = str(configured or "").strip()
    if text:
        return Path(text).expanduser()
    if LEGACY_ROOT.is_dir():
        return LEGACY_DATA_ROOT
    return Path(fallback) if fallback else _fallback_root()


def _resolve_bin(configured: str) -> str:
    """按「配置 → 历史路径 → PATH → 数据根 bin」的顺序定出 javdb 二进制路径。"""
    text = str(configured or "").strip()
    if text:
        return text
    if os.access(str(LEGACY_JAVDB), os.X_OK):
        return str(LEGACY_JAVDB)
    found = shutil.which("javdb")
    if found:
        return found
    return str(data_root() / "bin" / "javdb")


def configure(data_root: str = "", javdb_bin: str = "",
              fallback_root: Optional[Path] = None) -> None:
    """锁定本次运行的数据根与 javdb 路径；由 `init_plugin()` 调用。

    :param data_root: 插件配置里的 `data_root`，留空按默认顺序解析
    :param javdb_bin: 插件配置里的 `javdb_bin`，留空按默认顺序解析
    :param fallback_root: 宿主插件数据目录（`_PluginBase.get_data_path()`），最后的回落
    """
    global _DATA_ROOT, _JAVDB_BIN
    _DATA_ROOT = _resolve_root(data_root, fallback_root)
    _JAVDB_BIN = _resolve_bin(javdb_bin)


def data_root() -> Path:
    """数据根目录（SQLite 台账、JSON 缓存、图片缓存同根）。"""
    return _DATA_ROOT if _DATA_ROOT is not None else _fallback_root()


def meta_dir() -> Path:
    """元数据目录：JSON 缓存与插件自管 SQLite 都落在这里。"""
    return data_root() / "meta"


def db_path() -> Path:
    """插件自管 SQLite 台账的完整路径。"""
    return meta_dir() / "avhelper.db"


def images_dir() -> Path:
    """图片缓存目录（`/img` 代理与预热封面落盘处）。"""
    return data_root() / "images"


def bin_dir() -> Path:
    """外部二进制目录（自动下载的 javdb 落在这里）。"""
    return data_root() / "bin"


# 随包内置的资源目录（映射表、简繁词典等）：`plugins.v3/avhelper/resources/`。
# 与「数据根」分开：这些文件是**发行的代码资产**，随插件一起分发、不随用户数据变化；
# 用户想覆盖某一份时，把同名文件放进 `{data_root}/resources/` 即可（见 resource_file）。
BUILTIN_RESOURCES = Path(__file__).resolve().parent / "resources"


def resources_dir() -> Path:
    """资源目录：用户覆盖目录存在就用它，否则用随包内置的那份。

    只判断**目录是否已存在**：`{data_root}/resources` 是我们自己不会创建的位置，
    它一旦存在就一定是用户手工放的（打包与测试都不会凭空生成），可以放心当覆盖信号。
    """
    override = data_root() / "resources"
    return override if override.is_dir() else BUILTIN_RESOURCES


def resource_file(name: str) -> Path:
    """按「用户覆盖 → 内置」逐文件解析一份资源，返回第一个**已存在**的路径。

    逐文件而不是逐目录：用户可能只覆盖 `zhcdict.json` 一份，另外两份仍旧走内置。
    两份都没有时返回内置路径（调用方按「文件不存在」处理，不要在这里抛异常 ——
    资源缺失只该让刮削降级，不该让插件起不来）。
    """
    text = str(name or "").strip()
    if not text:
        return BUILTIN_RESOURCES
    override = data_root() / "resources" / text
    if override.is_file():
        return override
    return BUILTIN_RESOURCES / text


def cache_limit_gb(value: Any = None) -> float:
    """把「缓存上限（GB）」配置归一成一个安全值（默认 `DEFAULT_CACHE_MAX_GB`）。

    插件配置里这一个键来自用户输入（设置台「运行与缓存」的输入框，或宿主 JSON 表单），
    所以四种脏数据都要接住：空串 / 缺键 / 非数字 / 负数或 0。

    **0 与负数一律回落默认值，不是「不限制」**：淘汰逻辑拿它当字节上限用，
    归零等于每晚 4 点把缓存目录整个清空（`is_keep_file()` 只保得住 SQLite）。
    真要「不限制」就填一个大数，上限被夹在 `CACHE_MAX_GB_MAX`。

    :param value: 配置里的原始值（可能是 `"1"` / `2.5` / `None` / 乱码）
    :return: 夹在 [`CACHE_MAX_GB_MIN`, `CACHE_MAX_GB_MAX`] 里的 GB 数
    """
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_CACHE_MAX_GB
    if not math.isfinite(parsed) or parsed <= 0:
        return DEFAULT_CACHE_MAX_GB
    return min(max(parsed, CACHE_MAX_GB_MIN), CACHE_MAX_GB_MAX)


def cache_limit_bytes(value: Any = None) -> int:
    """缓存上限的字节数形式（`cache_limit_gb()` 的口径，给淘汰与界面共用）。"""
    return int(cache_limit_gb(value) * 1024 ** 3)


def javdb_bin() -> str:
    """javdb 可执行文件路径（可能尚不存在，用 `javdb_ready()` 判断）。"""
    return _JAVDB_BIN if _JAVDB_BIN else str(LEGACY_JAVDB)


def javdb_ready() -> bool:
    """javdb 二进制是否已就位且可执行。"""
    return os.access(javdb_bin(), os.X_OK)


def is_managed_javdb(bin_path: Optional[str] = None) -> bool:
    """二进制是否落在**自动管理路径**（`data_root/bin/javdb`）——只有它允许自动覆盖升级。

    手工指定的 `javdb_bin` / 历史挂载卷 / PATH 上的二进制一律不是：尊重用户的路径
    选择是 0.4.0 起的约定，「检测更新 → 升级」按钮对它们只提示不代劳。
    """
    current = str(bin_path or javdb_bin())
    try:
        return Path(current).resolve() == (bin_dir() / "javdb").resolve()
    except OSError:
        return False


def _platform_tag() -> Tuple[str, str]:
    """当前平台对应的 (GOOS, GOARCH)；不认识的平台返回两个空串。"""
    return (_SYSTEMS.get(platform.system().lower(), ""),
            _MACHINES.get(platform.machine().lower(), ""))


def _pick_asset(version: str = JAVDB_VERSION) -> Tuple[str, str]:
    """算出 (Release 资源文件名, 压缩格式)；平台不受支持时返回两个空串。"""
    system, machine = _platform_tag()
    if not system or not machine:
        return "", ""
    ext = "zip" if system == "windows" else "tar.gz"
    name = f"javdb-cli_{version}_{system}_{machine}.{ext}"
    return name, ext


def _download(url: str, proxy: str, timeout: int = 120) -> bytes:
    """下载一个 URL 的字节；给了代理就走代理，不给就用系统默认直连。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "AVHelper"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _extract_archive(blob: bytes, ext: str, target: Path) -> bool:
    """把压缩包里的 javdb 可执行文件解到 target 并加上执行位；失败返回 False。"""
    suffix = ".zip" if ext == "zip" else ".tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / f"javdb{suffix}"
        archive.write_bytes(blob)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if ext == "zip":
                with zipfile.ZipFile(archive) as zf:
                    member = next((n for n in zf.namelist()
                                   if Path(n).name in ("javdb", "javdb.exe")), "")
                    if not member:
                        return False
                    with zf.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())
            else:
                with tarfile.open(archive, "r:gz") as tf:
                    member = next((m for m in tf.getmembers()
                                   if m.isfile() and Path(m.name).name == "javdb"), None)
                    if member is None:
                        return False
                    src = tf.extractfile(member)
                    if src is None:
                        return False
                    with src, open(target, "wb") as dst:
                        dst.write(src.read())
        except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError):
            return False
    target.chmod(0o755)
    return True


def _version_marker(bin_path: str) -> Path:
    """版本标记文件：下载成功后写在二进制旁边（`bin/javdb.version`）。"""
    return Path(bin_path).parent / "javdb.version"


def installed_version(bin_path: str) -> str:
    """现有二进制的版本号；认不出给空串（= 未知，调用方不拿它做决定）。

    先读下载时写的标记文件（零开销）；标记缺失（存量部署在 0.9.5 及更早下载的、
    手工放置的）退回跑一次 `javdb --version`（best-effort，8 秒超时，只在本插件的
    守护线程里发生，不阻塞宿主）。
    """
    marker = _version_marker(bin_path)
    try:
        text = marker.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", text):
            return text
    except OSError:
        pass
    try:
        proc = subprocess.run([str(bin_path), "--version"], capture_output=True,
                              timeout=8, stdin=subprocess.DEVNULL)
        out = (proc.stdout or b"") + (proc.stderr or b"")
        found = re.search(r"\b(\d+\.\d+\.\d+)\b", out.decode("utf-8", "replace"))
        return found.group(1) if found else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _mark_installed(bin_path: str, version: str) -> None:
    """下载成功后落版本标记（失败不影响主流程——标记缺失只是下次多跑一次 --version）。"""
    try:
        _version_marker(bin_path).write_text(version, encoding="utf-8")
    except OSError:
        pass


def _version_tuple(text: str) -> Optional[Tuple[int, ...]]:
    """"0.8.4" → (0, 8, 4)；解析失败给 None（调用方不拿未知做比较决定）。"""
    found = re.fullmatch(r"v?(\d+(?:\.\d+){1,3})", str(text or "").strip())
    if not found:
        return None
    return tuple(int(part) for part in found.group(1).split("."))


def version_at_least(installed: str, floor: str) -> bool:
    """installed ≥ floor（逐段数值比较，容忍 0.8 / 0.8.4 / 0.8.4.1 这类长度差）。"""
    left, right = _version_tuple(installed), _version_tuple(floor)
    if not left or not right:
        return False
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left >= right


def fetch_latest_version(proxy: str = "", timeout: int = 30) -> Tuple[Optional[str], str]:
    """查上游 latest Release 的版本号（GitHub API）；失败给 (None, 原因)。

    调用方自己缓存（TTL 6h），这里不发第二次。rate limit：未认证 60 次/时/IP，
    每天几次的频率远够。
    """
    url = f"https://api.github.com/repos/{JAVDB_REPO}/releases/latest"
    try:
        blob = _download(url, proxy, timeout=timeout)
        data = json.loads(blob.decode("utf-8", "replace"))
    except Exception as error:                                      # noqa: BLE001
        return None, f"{type(error).__name__}: {error}"
    tag = str((data or {}).get("tag_name") or "")
    parsed = _version_tuple(tag)
    if not parsed:
        return None, f"latest 响应异常：tag_name={tag!r}"
    # 返回**裸版本**（与版本标记/install_version 同口径，不带 v 前缀）
    return ".".join(str(part) for part in parsed), ""


def ensure_javdb(proxy: str = "") -> Tuple[bool, str]:
    """确保 javdb 二进制可用且**不低于下限版本**；缺文件或低于下限时自动下载。

    版本策略（2026-11-20 用户定的修订，替代纯锁定）：

    - 缺文件 → 装 `JAVDB_VERSION`（推荐版本，实测过）；
    - 已装版本 **< `JAVDB_MIN_VERSION`**（实测已坏，如 0.8.2）→ 自动升级到推荐版本——
      这种情况下旧版等于功能坏死，不能等用户发现再动手；
    - 已装 ≥ 下限但 ≠ 推荐版本（用户在「检测更新」里自选装的新版）→ **不动**，
      只在日志里说一句「与本插件实测版本不同」；
    - 以上仅对**自动管理路径**（`data_root/bin/javdb`）生效；手工指定路径永不覆盖，
      只提示。

    :param proxy: 下载用的代理（内地直连 GitHub 通常不通，沿用插件里那份代理配置）
    :return: (是否可用, 说明文本)；说明文本用于日志与界面提示，失败原因不吞掉
    """
    current = javdb_bin()
    if javdb_ready():
        installed = installed_version(current)
        if not installed:
            return True, current                       # 版本未知（跑不了 --version）：别动它
        if version_at_least(installed, JAVDB_MIN_VERSION):
            note = "" if installed == JAVDB_VERSION else \
                f"（{installed} ≥ 下限 {JAVDB_MIN_VERSION}，保持不动；" \
                f"与本插件实测的 {JAVDB_VERSION} 不同，遇到兼容问题请切回）"
            return True, f"{current}{note}"
        if is_managed_javdb(current):
            # 自动管理路径 + 低于下限 → 必须升级（下载失败时旧二进制还在，不会更差）
            ok, detail = install_version(JAVDB_VERSION, proxy,
                                         reason=f"已装 {installed} < 下限 {JAVDB_MIN_VERSION}")
            return (ok, f"{detail}（自动升级：已装 {installed} → {JAVDB_VERSION}）"
                    if ok else detail)
        return True, (f"{current}（版本 {installed} 低于下限 {JAVDB_MIN_VERSION}，"
                      "手工路径不自动升级；请自行更换或清空该配置）")
    return install_version(JAVDB_VERSION, proxy)


def install_version(version: str, proxy: str = "",
                    reason: str = "") -> Tuple[bool, str]:
    """下载并安装**指定版本**到 `javdb_bin()`，写版本标记（「检测更新→升级」与下限升级共用）。

    :return: (是否成功, 成功时为二进制路径、失败时为原因)——**成功时是纯路径**，
    调用方与既有测试把它当路径用；版本信息由调用方补进日志。
    """
    current = javdb_bin()
    parsed = _version_tuple(version)
    if not parsed:
        return False, f"版本号 {version!r} 不合法"
    plain = ".".join(str(part) for part in parsed)     # "v0.8.4" → "0.8.4"
    asset, ext = _pick_asset(plain)
    system, machine = _platform_tag()
    if not asset:
        return False, (f"当前平台（{platform.system()}/{platform.machine()}）"
                       "没有对应的 javdb 预编译包，请手动指定 javdb_bin")
    url = (f"https://github.com/{JAVDB_REPO}/releases/download/"
           f"v{plain}/{asset}")
    target = Path(current)
    try:
        blob = _download(url, proxy)
    except Exception as error:  # 下载失败不能连累插件其它功能
        return False, (f"下载 javdb-cli {plain} 失败（{type(error).__name__}: {error}）"
                       + (f"（{reason}）" if reason else "")
                       + "，可在插件设置里手动指定 javdb_bin")
    if not _extract_archive(blob, ext, target):
        return False, (f"javdb-cli（{system}/{machine}）压缩包解包失败"
                       + (f"（{reason}）" if reason else "")
                       + "，可手动指定 javdb_bin")
    _mark_installed(current, plain)
    return True, str(target)


def _download_and_install(proxy: str, reason: str = "") -> Tuple[bool, str]:
    """兼容旧名的薄壳：装推荐版本。"""
    return install_version(JAVDB_VERSION, proxy, reason)
