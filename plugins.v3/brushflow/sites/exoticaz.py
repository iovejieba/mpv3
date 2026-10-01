"""ExoticaZ (exoticaz.to)：全站 H&R 规则适配（公式为站点官方规则）。

做种时长公式（x = 种子体积，GiB；站点页面展示的 GB 即 GiB）：

    f(x) = 72 + 2x               , x < 50
    f(x) = 100·ln(x) − 219.2023  , x ≥ 50

两段在 50GB 处近似连续（100·ln50 − 219.2023 = 172.00000054…，ceil 后为 173）；
分支条件必须 x < 50（右支含 50）；向上取整；体积未知按站点最低 72h 兜底。
豁免：做种满公式时长，或该种 ratio ≥ 豁免线；比较必须用 ceil 后的整数值。
站点官方豁免线为 0.9；内置默认 1.0 为 qB 本地统计与站点记账之间的上报偏差留缓冲。

本模块同时提供下载校验与体积/ID 解析的纯函数（与参考实现逐行对齐），
供删种载荷校验与未来的 RSS/详情富化通道复用；对拍基线为
MoviePilot-Resources-main/exoticaz-python-reference/hr_conformance_vectors.json（31 条）。
"""

import math
import re

from . import SiteHRRule, register_rule, register_stats_parser

SITE_DOMAIN = "exoticaz.to"
SITE_MIN_CLEAR_RATIO = 0.9      # 站点官方规则，配置硬下限（0 表示关闭分享率通道）
SITE_DEFAULT_CLEAR_RATIO = 1.0  # 内置默认（官方 0.9 + 上报偏差缓冲）

_SIZE_RE = re.compile(r"(?i)^([\d.,]+)\s*([KMGTP])i?B")
_TORRENT_ID_RE = re.compile(r"(?:/torrent/|^)(\d+)")


def hr_seed_hours(size_gib: float) -> int:
    """官方 H&R 公式：x 为体积（GiB），返回所需做种小时（先 max 后 ceil）。"""
    if size_gib is None or size_gib <= 0:
        return 72
    hours = 72 + 2 * size_gib if size_gib < 50 else 100 * math.log(size_gib) - 219.2023
    return math.ceil(max(hours, 72))


def is_hr_cleared(size_gib: float, seeded_hours: float, ratio: float,
                  clear_ratio: float = SITE_MIN_CLEAR_RATIO) -> bool:
    """豁免判定（参考实现对齐版）：做种满公式时长 或 分享率 >= 豁免线，任一达标。"""
    return seeded_hours >= hr_seed_hours(size_gib) or (clear_ratio > 0 and ratio >= clear_ratio)


def norm_space(text: str) -> str:
    """空白归一化（站点 HTML 词间常有换行，匹配前必须先做）。"""
    return " ".join(text.split())


def parse_size_bytes(text: str) -> int:
    """解析行首体积："2.65 GB" → 字节；"25.68 TB 26,932,182 MB" → 取行首。

    1024 进制（站点展示的 GB 即 GiB）。⚠️ 上传/下载行内嵌 MB 徽标，必须锚定行首。
    """
    m = _SIZE_RE.match(norm_space(text))
    if not m:
        return 0
    value = float(m.group(1).replace(",", ""))
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50}[m.group(2).upper()]
    return int(value * mult)


def torrent_id_from_path(text: str) -> str:
    """/torrent/196268、/torrent/196268-78e1...、"196268" → "196268"。"""
    m = _TORRENT_ID_RE.search(text.strip())
    return m.group(1) if m else ""


def validate_torrent_payload(data: bytes) -> bool:
    """下载内容校验：bittorrent 文件以 bencode dict 'd' 开头；HTML 说明登录态失效。"""
    return bool(data) and data[:1] == b"d"


def validate_torrent_payload(data: bytes) -> bool:
    """下载内容校验：bittorrent 文件以 bencode dict 'd' 开头；HTML 说明登录态失效。"""
    return bool(data) and data[:1] == b"d"


# 登录页用户统计解析复用收编的账本解析器（lxml 实现，与 MP 侧 userdata 插件同源）：
# 字段 username/upload/download/ratio/bonus/seeding/leeching；未登录返回 None。
from .exoticaz_ledger import parse_ratio_bar as _vendored_parse_ratio_bar  # noqa: E402

register_rule(SiteHRRule(
    domain=SITE_DOMAIN,
    required_hours=hr_seed_hours,
    default_clear_ratio=SITE_DEFAULT_CLEAR_RATIO,
    min_clear_ratio=SITE_MIN_CLEAR_RATIO,
    window_hours=96.0,  # 做种窗口：完成时刻起 4 天（2026-09 history 样本实证）
))
register_stats_parser(SITE_DOMAIN, _vendored_parse_ratio_bar)
