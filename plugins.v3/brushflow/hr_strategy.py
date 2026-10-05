# -*- coding: utf-8 -*-
"""站点 H&R 策略层 + 用户面板解析（平铺单文件，消除子包结构）。

合并自原 sites/__init__.py + sites/exoticaz.py + sites/exoticaz_ledger.py。
设计依据：ExoticaZ-HR适配参考 + MPV3交叉验证说明 + 刷流插件对接说明。
对拍基线：hr_conformance_vectors.json（56ADE6A0）。
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlsplit

from lxml import etree

# ================================================================ 常量

GIB = 1 << 30
MIN_REQUEST_INTERVAL = 1.0  # 站点请求纪律 ≤1 rps
EXOTICAZ_DOMAIN_SUFFIX = "exoticaz.to"

# ================================================================ 域名归一化


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


def is_exoticaz(url: str) -> bool:
    netloc = urlsplit(str(url or "")).netloc.lower().split(":")[0]
    return netloc == EXOTICAZ_DOMAIN_SUFFIX or netloc.endswith("." + EXOTICAZ_DOMAIN_SUFFIX)


def base_url_of(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else str(url or "").rstrip("/")


# ================================================================ SiteHRRule 策略对象


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
        """合并配置覆盖；低于站点规则下限的配置一律按更保守值处理。"""
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

# 站点用户统计解析器（domain → callable(html) -> Optional[dict]）
STATS_PARSERS: Dict[str, Callable] = {}


def register_rule(rule: SiteHRRule) -> SiteHRRule:
    REGISTRY[rule.domain.strip().lower()] = rule
    return rule


def register_stats_parser(domain: str, parser: Callable) -> Callable:
    STATS_PARSERS[str(domain).strip().lower()] = parser
    return parser


def resolve_rule(domain: Optional[str]) -> Optional[SiteHRRule]:
    if not domain:
        return None
    return REGISTRY.get(normalize_domain(domain))


def resolve_stats_parser(domain: Optional[str]) -> Optional[Callable]:
    if not domain:
        return None
    return STATS_PARSERS.get(normalize_domain(domain))


# ================================================================ 文本解析（参考实现对齐）


def norm_space(text: str) -> str:
    """空白归一化（站点 HTML 词间常有换行，匹配前必须先做）。"""
    return " ".join(str(text or "").split())


_SIZE_RE = re.compile(r"(?i)^([\d.,]+)\s*([KMGTP])i?B")
_NUM_RE = re.compile(r"[\d.,]+")
_TORRENT_ID_RE = re.compile(r"(?:/torrent/|^)(\d+)")


def parse_size_bytes(text: str) -> int:
    """1024 进制（GB=GiB），行首锚定。"""
    m = _SIZE_RE.match(norm_space(text))
    if not m:
        return 0
    value = float(m.group(1).replace(",", ""))
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50}[m.group(2).upper()]
    return int(value * mult)


def parse_number(text: str) -> float:
    """提取首个数字（含千分位逗号）；无数字返回 0.0（含 "..." 占位符守卫）。"""
    cleaned = norm_space(text)
    if not re.search(r"\d", cleaned):
        return 0.0
    m = _NUM_RE.search(cleaned)
    return float(m.group(0).replace(",", "")) if m else 0.0


# 兼容别名：账本/采集实现内部原以 _parse_number 引用（含 "..." 守卫）。
_parse_number = parse_number


def torrent_id_from_path(text: str) -> str:
    """/torrent/196268 → "196268"。"""
    m = _TORRENT_ID_RE.search(str(text or "").strip())
    return m.group(1) if m else ""


def validate_torrent_payload(data: bytes) -> bool:
    """bittorrent 文件以 bencode dict 'd' 开头；HTML 说明登录态失效。"""
    return bool(data) and data[:1] == b"d"


# ================================================================ ExoticaZ H&R 公式与站点注册


def hr_seed_hours(size_gib: float) -> int:
    """官方 H&R 公式：x 为体积（GiB），返回所需做种小时（先 max 后 ceil）。

    f(x) = 72 + 2x               , x < 50
    f(x) = 100·ln(x) − 219.2023  , x ≥ 50
    两段在 50GB 处近似连续（100·ln50−219.2023 = 172.00000054…，ceil 后为 173）；
    分支条件必须 x < 50（右支含 50）；体积未知按站点最低 72h 兜底。
    """
    if size_gib is None or size_gib <= 0:
        return 72
    hours = 72 + 2 * size_gib if size_gib < 50 else 100 * math.log(size_gib) - 219.2023
    return math.ceil(max(hours, 72))


def is_hr_cleared(size_gib: float, seeded_hours: float, ratio: float,
                  clear_ratio: float = 0.9) -> bool:
    """豁免判定（参考实现对齐版）：做种满公式时长 或 分享率 >= 豁免线，任一达标。"""
    return seeded_hours >= hr_seed_hours(size_gib) or (clear_ratio > 0 and ratio >= clear_ratio)


SITE_DOMAIN = "exoticaz.to"
SITE_MIN_CLEAR_RATIO = 0.9
SITE_DEFAULT_CLEAR_RATIO = 1.0


def _make_exoticaz_rule() -> SiteHRRule:
    return SiteHRRule(
        domain=SITE_DOMAIN,
        required_hours=hr_seed_hours,
        default_clear_ratio=SITE_DEFAULT_CLEAR_RATIO,
        min_clear_ratio=SITE_MIN_CLEAR_RATIO,
        window_hours=96.0,
    )


register_rule(_make_exoticaz_rule())


# ================================================================ 用户面板解析（lxml，收编自 userdata_parser）


def _tooltip_text(el) -> str:
    """取 tooltip 文本：兼容服务端原始 HTML(title) 与浏览器另存 DOM
    (Bootstrap 初始化后把 title 搬进 data-original-title / data-bs-original-title)。"""
    return (el.get("data-original-title")
            or el.get("data-bs-original-title")
            or el.get("title")
            or "")


def parse_ratio_bar(html_text: str) -> Optional[Dict[str, Any]]:
    """解析任意登录页顶部 .ratio-bar。返回 None 表示未登录(页面缺少 ratio-bar)。"""
    html = etree.HTML(html_text or "")
    if html is None:
        return None
    bars = html.xpath("//*[contains(@class, 'ratio-bar')]")
    if not bars:
        return None
    bar = bars[0]
    text = norm_space(bar.xpath("string(.)"))
    out: Dict[str, Any] = {}
    links = bar.xpath(".//a[contains(@href, '/profile/')]")
    if links:
        out["username"] = norm_space(links[0].xpath("string(.)"))
    for key, title in (("upload", "Upload"), ("download", "Download"), ("ratio", "Ratio")):
        nodes = bar.xpath(f".//div[@title='{title}']")
        if nodes:
            value = norm_space(nodes[0].xpath("string(.)"))
            out[key] = parse_number(value) if key == "ratio" else parse_size_bytes(value)
    bonus = re.search(r"Bonus:\s*([\d.,]+)", text)
    if bonus:
        out["bonus"] = parse_number(bonus.group(1))
    seeding = re.search(r"Seeding:\s*(\d+)", text)
    if seeding:
        out["seeding"] = int(seeding.group(1))
    leeching = re.search(r"Leeching:\s*(\d+)", text)
    if leeching:
        out["leeching"] = int(leeching.group(1))
    groups = bar.xpath(".//span[contains(@class, 'user-group')]")
    if groups:
        out["user_level"] = norm_space(groups[-1].xpath("string(.)"))
    return out


def parse_profile(html_text: str) -> Dict[str, Any]:
    """解析 /profile/{username} 信息表: Rank -> 等级, Joined -> 注册时间。"""
    html = etree.HTML(html_text or "")
    out: Dict[str, Any] = {}
    if html is None:
        return out
    for tr in html.xpath("//tr"):
        cells = tr.xpath("./td")
        if len(cells) < 2:
            continue
        label = norm_space(cells[0].xpath("string(.)"))
        value = norm_space(cells[-1].xpath("string(.)"))
        if label == "Rank" and value:
            out["user_level"] = value
        elif label == "Joined" and value:
            out["join_at"] = norm_space(value.split("(")[0])
    return out


def parse_active_page(html_text: str) -> Tuple[int, int, List[Tuple[int, int]], Optional[str]]:
    """解析做种列表页。返回 (本页条数, 本页体积字节, [(做种人数, 体积)...], 下一页URL或None)。

    表头自适应: 存在 class 含 size/seeders 的 th 时按其列序定位(与官方 Unit3d parser 一致)。

    2026-09-30 实机修复（exoticaz 线上 /profile/{u}/active 触发 ValueError: '...'）：
    - 做种数单元格会渲染 "..." 占位符（懒加载/隐藏数据），_NUM_RE 字符类含点号会
      命中它 → float("...") 炸采集；_parse_number 已加"无数字返回 0"守卫；
    - 逐行取列：原先 sizes/seeders 两列各自收集再按序号配对，行内缺 td（空状态
      colspan 行等）会两列错位甚至越界；
    - 多个 seeders th 时取第一个（补 [1] 锚点，与 size 列探测对称）。
    """
    html = etree.HTML(html_text or "")
    if html is None or len(html) == 0:
        return 0, 0, [], None
    size_col, seeders_col = 9, 2
    if html.xpath('//thead//th[contains(@class,"size")]'):
        size_col = len(html.xpath('//thead//th[contains(@class,"size")][1]/preceding-sibling::th')) + 1
    if html.xpath('//thead//th[contains(@class,"seeders")]'):
        seeders_col = len(html.xpath('//thead//th[contains(@class,"seeders")][1]/preceding-sibling::th')) + 1
    total, info, count = 0, [], 0
    for tr in html.xpath("//tr"):
        cells = tr.xpath("./td")
        if len(cells) < max(size_col, seeders_col):
            continue
        size = parse_size_bytes(cells[size_col - 1].xpath("string(.)"))
        seed = int(parse_number(cells[seeders_col - 1].xpath("string(.)")))
        if size <= 0 and seed <= 0:
            continue
        count += 1
        total += size
        info.append((seed, size))
    next_page = None
    pages = html.xpath('//ul[@class="pagination"]/li[contains(@class,"active")]/following-sibling::li')
    for li in pages:
        num = norm_space(li.xpath("string(.)"))
        if num.isdigit():
            next_page = num
            break
    return count, total, info, next_page


class RequestThrottle:
    """简单节流器: 相邻请求间隔 >= MIN_REQUEST_INTERVAL。"""

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL):
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delta = time.time() - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.time()


def collect_user_data(fetch: Callable[..., Optional[Any]],
                      base_url: str,
                      throttle: Optional[RequestThrottle] = None) -> Dict[str, Any]:
    """完整采集编排。fetch(url) -> (status, text) 或 None。

    返回 dict:
      登录失效  -> {"err_msg": "..."}  (无 userid, 宿主不持久化)
      成功      -> username/userid/user_level/join_at/bonus/upload/download/ratio/
                   seeding/leeching/seeding_size/seeding_info
    """
    throttle = throttle or RequestThrottle()
    result: Dict[str, Any] = {}

    def get(path: str) -> Tuple[Optional[int], str]:
        throttle.wait()
        resp = fetch(urljoin(base_url + "/", path.lstrip("/")))
        if resp is None:
            return None, ""
        return getattr(resp, "status_code", None), getattr(resp, "text", "") or ""

    status, home_html = get("/")
    if status is None:
        result["err_msg"] = "网络请求失败，无法连接站点"
        return result
    body = (home_html or "").lower()
    if status in (401, 403) and any(m in body for m in ("cloudflare", "just a moment")):
        result["err_msg"] = "已被 Cloudflare 拦截，请人工过盾后再刷新"
        return result
    bar = parse_ratio_bar(home_html)
    if bar is None or not bar.get("username"):
        result["err_msg"] = "登录态失效：页面缺少 ratio-bar，请检查 Cookie 是否有效"
        return result
    result.update({
        "username": bar.get("username"),
        "userid": bar.get("username"),
        "user_level": bar.get("user_level") or "",
        "join_at": "",
        "bonus": float(bar.get("bonus") or 0.0),
        "upload": int(bar.get("upload") or 0),
        "download": int(bar.get("download") or 0),
        "ratio": float(bar.get("ratio") or 0.0),
        "seeding": int(bar.get("seeding") or 0),
        "leeching": int(bar.get("leeching") or 0),
        "seeding_size": 0,
        "seeding_info": [],
    })
    username = result["username"]
    _, profile_html = get(f"/profile/{username}")
    profile = parse_profile(profile_html)
    if profile.get("user_level") and not result["user_level"]:
        result["user_level"] = profile["user_level"]
    result["join_at"] = profile.get("join_at", "")
    page = 1
    while page <= 50:
        _, active_html = get(f"/profile/{username}/active?perPage=100&page={page}")
        try:
            _, total, info, next_num = parse_active_page(active_html)
        except Exception:  # 做种段异常只降级（DESIGN.md §7），不炸整个采集
            result["seeding_degraded"] = True
            break
        result["seeding_size"] = int(result.get("seeding_size") or 0) + total
        result["seeding_info"] = list(result.get("seeding_info") or []) + info
        if not next_num:
            break
        page = int(next_num)
    return result


# ---------------------------------------------------------------- H&R 账本(history 页)

# 站点规则（合同以 刷流插件对接说明.md §2.3 为准，2026-09-30 实证）:
#   要求 = ceil(max(f(size), 72))，f 同参考实现 —— 与 _hr_required_hours 一致;
#   做种窗口: 要求须在下载完成后 W = 96 小时（4 天）内达成，锚点 = 完成时刻
#   （不是添加时刻）；站点按小时 cron 巡检（≤1h 粒度），窗口到期未达标 ->
#   即使仍在做种也记账为 H&R（tooltip: "Counted as a Hit & Run:
#   connected, but past its seeding window. Keep seeding, X left"）——二开差异点；
#   离线消耗窗口预算但不计入做种时长（实测漂移 ≈6h：离线 ~5h + announce 结算滞后 ~1h）；
#   做种中未到期 -> "Hit & Run not fulfilled but not counted for actively seeded torrent";
#   hnr=1 过滤视图 = 未达标观察名单（含已记账行），不是"已命中"历史;
#   已记账可自愈：继续做种到倒计时归零自动移出名单；BP 清除 = round(10 × 剩余要求小时);
#   倒计时按 announce 结算（announce 之间冻结），删种决定前应 force re-announce。


def parse_history_ledger(html_text: str) -> List[Dict[str, Any]]:
    """解析 /profile/{username}/history 账本页, 逐种返回服务器侧 H&R 状态。

    每行 dict 字段:
      torrent_id                                   种子 ID（从 /torrent/{id} 链接提取）
      title/size_bytes/seeders/leechers/completed  种子与文件信息
      uploaded/downloaded/ratio                    本种内你的上传/下载(字节)/分享率
      download_credited                            下载是否被记账豁免(Credited Download 徽标)
      add_hours_ago/updated_hours_ago              下载距今/最近 announce 距今(小时)
      seed_hours                                   服务器累计做种时长(由 H&R 倒计时反推, 精确;
                                                   无倒计时时为 None, seed_display 仅有天级截断值)
      hr_state                                     'none' | 'watch'(未达标, 做种中, 窗口内)
                                                   | 'counted'(已记账, 窗口期已过)
      hr_remaining_hours                           剩余要求时长(站点倒计时原文, 分钟精度)
      hr_clear_bp                                  花魔力清除的标价(仅 counted 态, 无则 None)
    """
    html = etree.HTML(html_text or "")
    if html is None:
        return []
    out: List[Dict[str, Any]] = []
    for tr in html.xpath("//table//tbody//tr"):
        cells = tr.xpath("./td")
        if len(cells) < 11:
            continue
        row: Dict[str, Any] = {
            "torrent_id": "",
            "title": "", "size_bytes": 0, "seeders": 0, "leechers": 0, "completed": 0,
            "uploaded": 0, "downloaded": 0, "ratio": 0.0, "download_credited": False,
            "add_hours_ago": None, "updated_hours_ago": None,
            "seed_hours": None, "seed_display": "",
            "hr_state": "none", "hr_remaining_hours": None, "hr_clear_bp": None,
        }
        file_cell = cells[1]
        row["title"] = norm_space(file_cell.xpath("string(.//a[contains(@href,'/torrent/')][1])"))
        torrent_link = file_cell.xpath(".//a[contains(@href,'/torrent/')][1]/@href")
        if torrent_link:
            m_id = re.search(r"/torrent/(\d+)", str(torrent_link[0]))
            row["torrent_id"] = m_id.group(1) if m_id else ""
        tail = norm_space(file_cell.xpath("string(.)"))
        m = re.search(r"(\d+)\s+(\d+)\s+(\d+)\s+([\d.,]+\s*[KMGTP]i?B)\s*$", tail, re.IGNORECASE)
        if m:
            row["seeders"], row["leechers"], row["completed"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
            row["size_bytes"] = parse_size_bytes(m.group(4))
        row["uploaded"] = parse_size_bytes(cells[4].xpath("string(.)"))
        dl_parts = norm_space(cells[5].xpath("string(.)")).split()
        row["downloaded"] = parse_size_bytes(dl_parts[0] if dl_parts else "")
        row["download_credited"] = bool(cells[5].xpath(".//span[contains(@data-original-title,'Credited')]")
                                        or cells[5].xpath(".//span[@title][contains(@title,'Credited')]"))
        row["ratio"] = parse_number(cells[6].xpath("string(.)"))
        for idx, key in ((7, "add_hours_ago"), (8, "updated_hours_ago")):
            spans = cells[idx].xpath(".//span[@data-toggle='tooltip']")
            if spans:
                row[key] = _ago_to_hours(_tooltip_text(spans[0]))
        seed_spans = cells[9].xpath(".//span[@data-toggle='tooltip']")
        if seed_spans:
            row["seed_display"] = norm_space(cells[9].xpath("string(.)"))
        tip_nodes = [el for el in cells[10].xpath(".//*[@data-toggle='tooltip']")
                     if _tooltip_text(el)]
        if not tip_nodes:
            hr_text = norm_space(cells[10].xpath("string(.)"))
            if hr_text:
                row["hr_state"] = "watch"
                row["hr_remaining_hours"] = _remaining_to_hours(hr_text)
        for node in tip_nodes:
            tip = norm_space(_tooltip_text(node))
            if "Counted as a Hit & Run" in tip:
                row["hr_state"] = "counted"
                row["hr_remaining_hours"] = _remaining_to_hours(tip)
            elif "not counted" in tip or re.search(r"\d+\s*(hours?|minutes?).{0,20}left", tip, re.IGNORECASE):
                if row["hr_state"] == "none":
                    row["hr_state"] = "watch"
                    row["hr_remaining_hours"] = _remaining_to_hours(tip)
            bp = re.search(r"Clear this Hit & Run for (\d+) BP", tip)
            if bp:
                row["hr_clear_bp"] = int(bp.group(1))
        if row["hr_remaining_hours"] is not None and row["size_bytes"]:
            size_gib = row["size_bytes"] / (1 << 30)
            required = _hr_required_hours(size_gib)
            row["seed_hours"] = round(required - row["hr_remaining_hours"], 2)
        out.append(row)
    return out


def _hr_required_hours(size_gib: float) -> float:
    """站点 H&R 要求小时数(与参考实现 hr_seed_hours 同式, 整数小时, 用于倒计时反推)。"""
    if size_gib <= 0:
        return 72.0
    hours = 72 + 2 * size_gib if size_gib < 50 else 100 * math.log(size_gib) - 219.2023
    return float(math.ceil(max(hours, 72.0)))


def _ago_to_hours(text: str) -> Optional[float]:
    total = 0.0
    for n, unit in re.findall(r"(\d+)\s*(weeks?|days?|hours?|hrs?|minutes?|mins?|seconds?)", norm_space(text), re.IGNORECASE):
        n = int(n)
        u = unit.lower()
        if u.startswith("w"):
            total += n * 168
        elif u.startswith("d"):
            total += n * 24
        elif u.startswith("h"):
            total += n
        elif u.startswith("m"):
            total += n / 60
        else:
            total += n / 3600
    return total if total else None


def _remaining_to_hours(text: str) -> Optional[float]:
    m = re.search(r"(\d+)\s*hours?\s*(\d+)\s*minutes?\s*left", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    m = re.search(r"(\d+)\s*minutes?\s*(\d+)\s*seconds?\s*left", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) / 60 + int(m.group(2)) / 3600
    m = re.search(r"(\d+)\s*hours?\s*left", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


# ================================================================ 注册

register_rule(_make_exoticaz_rule())
register_stats_parser(SITE_DOMAIN, parse_ratio_bar)
