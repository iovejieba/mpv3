"""站点级刷流策略层：全站 H&R 等站点规则的可插拔适配。

本包保持纯逻辑：不导入宿主模块、不发起网络请求，可离线单元测试。
删种链在观测构造处调用 `resolve_rule(domain)`，豁免判定失败的种子
继续按 9.1 的 H&R 永久硬保护处理，硬安全线语义不因本包改变。
"""

from dataclasses import dataclass
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

GIB = 1 << 30


def normalize_domain(url: Optional[str]) -> str:
    """从 URL 或裸域名提取规范化域名（小写、去 www 前缀）；无法解析返回空串。"""
    if not url:
        return ""
    text = str(url).strip().lower()
    if "://" in text:
        text = urlparse(text).netloc
    if text.startswith("www."):
        text = text[4:]
    return text.split("/")[0].split(":")[0]


@dataclass(frozen=True)
class SiteHRRule:
    """全站 H&R 站点的删种保护规则。

    豁免语义与站点官方规则对齐，方向是"只多保、不早放"：
    做种时长 >= required_hours(体积)，或该种 ratio(上传/体积) >= clear_ratio。
    任何关键数据缺失都视为未豁免（fail-closed），维持 9.1 硬保护。

    window_hours：做种窗口——要求时长必须在下载完成后 W 小时内达成
    （ExoticaZ 实证 W=96h，锚点=完成时刻，离线消耗窗口但不计做种时长，
    到期未达标即使做种中也记账）。
    """

    domain: str
    required_hours: Callable[[float], int]
    default_clear_ratio: float
    min_clear_ratio: float
    window_hours: float = 96.0

    def clear_ratio(self, override: Optional[float] = None) -> float:
        """合并配置覆盖。

        与参考实现对齐（pt-tools hr_clear_ratio）：0 表示关闭分享率通道（最保守，
        只走做种时长路径）；(0, min_clear_ratio) 区间属于"比站点规则更激进"，
        一律按 min_clear_ratio 兜底；高于下限原样生效。
        """
        if override is None:
            return self.default_clear_ratio
        value = float(override)
        if value <= 0:
            return 0.0
        return max(value, self.min_clear_ratio)

    def required_seed_hours(self, size_bytes: float) -> int:
        """按体积（字节）返回所需做种小时；体积非法时按站点下限兜底。"""
        if size_bytes is None or float(size_bytes) <= 0:
            return self.required_hours(0)
        return self.required_hours(float(size_bytes) / GIB)

    def exempt(
        self,
        *,
        size_bytes: float,
        seeded_hours: float,
        uploaded_bytes: float,
        clear_ratio_override: Optional[float] = None,
    ) -> bool:
        """判定该种是否已解除 H&R；数据缺失或非法时返回 False（fail-closed）。"""
        try:
            size = float(size_bytes)
            hours = float(seeded_hours)
            uploaded = float(uploaded_bytes)
        except (TypeError, ValueError):
            return False
        if size <= 0 or hours < 0 or uploaded < 0:
            return False
        ratio = uploaded / size
        threshold = self.clear_ratio(clear_ratio_override)
        return hours >= self.required_seed_hours(size) or (threshold > 0 and ratio >= threshold)


REGISTRY: Dict[str, SiteHRRule] = {}

# 站点用户统计解析器（domain → callable(html) -> Optional[dict]）：
# MP 站点定义层不含用户数据（官方 Unit3d 站点同样如此），由插件按域名注册
# 自己的解析器做"登录页 ratio-bar 探活"，供站点分享率显示与控制兜底。
STATS_PARSERS: Dict[str, Callable] = {}


def register_rule(rule: SiteHRRule) -> SiteHRRule:
    """登记站点规则；同 domain 重复登记时后者覆盖（热重载安全）。"""
    REGISTRY[rule.domain.strip().lower()] = rule
    return rule


def register_stats_parser(domain: str, parser: Callable) -> Callable:
    """登记站点用户统计解析器；同 domain 重复登记时后者覆盖。"""
    STATS_PARSERS[str(domain).strip().lower()] = parser
    return parser


def resolve_stats_parser(domain: Optional[str]) -> Optional[Callable]:
    """按站点 domain 解析用户统计解析器；未登记返回 None（不做探活）。"""
    if not domain:
        return None
    cleaned = str(domain).strip().lower()
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    return STATS_PARSERS.get(cleaned)


def resolve_rule(domain: Optional[str]) -> Optional[SiteHRRule]:
    """按站点 domain 解析规则；未登记的站点返回 None（维持 9.1 现状）。"""
    if not domain:
        return None
    cleaned = str(domain).strip().lower()
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    return REGISTRY.get(cleaned)
