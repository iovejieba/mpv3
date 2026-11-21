"""候选种子打分：逐项可解释，数值与交互演示原型完全一致（执行方案 §6.5）。

打分只依赖种子标题/站点/做种数等已有字段，不请求网络；`cnsub` 与 `uncensored` 由标题正则判定，
正则先在真实站点种名上验证过（中字/中文/chs/简体/gb、无码/破解/uncensored/leak/流出）。
"""

import re
from typing import Any, Dict, List, Optional

# 标题特征正则（执行方案 §6.5 指定的特征，并按方案要求"先在真实种名上调好再上线"）
# 注意：chs/gb 必须加词边界——裸 `gb` 会命中 `GBR`（英国区码），把普通种子误判成有中字。
_CNSUB_RE = re.compile(r"中字|中文|简体|\bchs\b|\bgb\b", re.IGNORECASE)
_UNCENSORED_RE = re.compile(r"无码|破解|uncensored|leak|流出", re.IGNORECASE)
# 像素分辨率格式（`1920x1080` / `3840x2160`，兼容 × 与 , 千位分隔）——
# ExoticaZ 等站点的规格写在描述里且用像素格式，标题正则必须能识别（2026-11-20）。
_RES_2160_RE = re.compile(r"2160p|4k|uhd|3840[×x,]\s?2160|4096[×x,]\s?2160", re.IGNORECASE)
_RES_1080_RE = re.compile(r"1080[pi]|fhd|1920[×x,]\s?1080", re.IGNORECASE)
_RES_720_RE = re.compile(r"720p|1280[×x,]\s?720", re.IGNORECASE)
_H265_RE = re.compile(r"h\.?265|x265|hevc", re.IGNORECASE)
_H264_RE = re.compile(r"h\.?264|x264|avc", re.IGNORECASE)
_GROUP_RE = re.compile(r"-\[?([A-Za-z0-9@._-]{2,20})\]?\s*$")


def detect_resolution(title: str) -> str:
    """从种名判定分辨率，返回 2160p/1080p/720p 或空串。"""
    text = title or ""
    if _RES_2160_RE.search(text):
        return "2160p"
    if _RES_1080_RE.search(text):
        return "1080p"
    if _RES_720_RE.search(text):
        return "720p"
    return ""


def detect_cnsub(title: str) -> bool:
    """从种名判定是否带中文字幕。"""
    return bool(_CNSUB_RE.search(title or ""))


def detect_uncensored(title: str) -> bool:
    """从种名判定是否无码/破解版。"""
    return bool(_UNCENSORED_RE.search(title or ""))


def detect_codec(title: str) -> str:
    """从种名判定编码，未知时返回空串。"""
    text = title or ""
    if _H265_RE.search(text):
        return "H265"
    if _H264_RE.search(text):
        return "H264"
    return ""


def detect_group(title: str) -> str:
    """从种名尾部解析发布组，解析不到返回空串。"""
    match = _GROUP_RE.search((title or "").strip())
    return match.group(1) if match else ""


def publisher_label(cand: Dict[str, Any]) -> str:
    """拼「发布源」文案：站点（+ 发布组）。

    种名里解析不出发布组时旧版会兜底成「<站点> 发布」，再叠上模板自带的「 发布」就变成
    「馒头 / 馒头 发布 发布」；这里把与站点同名的兜底组去掉，只留站点，历史数据也一并纠正。
    """
    site = str(cand.get("site") or "").strip()
    group = str(cand.get("group") or "").strip()
    if group and group.replace(" ", "") in (site, site + "发布"):
        group = ""
    if not group:
        return site or "未知来源"
    if group.endswith("发布"):            # 「BT 匿名发布」这类整句，别再补一个「发布」
        return f"{site} / {group}" if site else group
    return f"{site} / {group} 发布" if site else f"{group} 发布"


def human_size(size_bytes: Any) -> str:
    """字节数转人类可读大小。"""
    try:
        value = float(size_bytes or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit in ("GB", "TB") else f"{value:.0f} {unit}"
        value /= 1024
    return "—"


def candidate_from_torrent(torrent: Any, pref_sites: Optional[List[str]] = None) -> Dict[str, Any]:
    """把一条搜索结果（宿主 TorrentInfo 或站点适配层返回的 dict）转成统一候选结构。

    站点适配层能给出权威元数据时（馒头成人区带 `standard`/`hasChineseSubtitle` 等），
    优先用 `*_hint` 字段；没有提示的站点退回种名/副标题正则。
    """
    title = str(_get(torrent, "title"))
    description = str(_get(torrent, "description"))
    site = str(_get(torrent, "site_name"))
    # 站点结构化标签（MP 索引器的 `labels`，如 ExoticaZ 的「1080p HD」）：站点自己打的
    # 规格标签，比自由文本标题/描述更可信。list/str 两种形状都接。
    labels_raw = _get(torrent, "labels")
    if isinstance(labels_raw, (list, tuple)):
        label_text = " ".join(str(x) for x in labels_raw if x)
    else:
        label_text = str(labels_raw or "")
    # 规格探测回落链：hint → 站点标签 → 标题 → 描述（ExoticaZ 等站点标题不带规格，
    # 规格在站点标签与描述文本里）
    res = (str(_get(torrent, "res_hint", ""))
           or detect_resolution(label_text)
           or detect_resolution(title)
           or detect_resolution(description))
    codec = (str(_get(torrent, "codec_hint", ""))
             or detect_codec(label_text)
             or detect_codec(title)
             or detect_codec(description)
             or "—")
    cnsub = (bool(_get(torrent, "cnsub_hint", False)) or detect_cnsub(label_text)
             or detect_cnsub(title) or detect_cnsub(description))
    uncensored = (bool(_get(torrent, "uncensored_hint", False))
                  or detect_uncensored(label_text)
                  or detect_uncensored(title) or detect_uncensored(description))
    try:
        seeders = int(_get(torrent, "seeders", 0) or 0)
    except (TypeError, ValueError):
        seeders = 0
    try:
        dvf = _get(torrent, "downloadvolumefactor", None)
        free = dvf is not None and float(dvf) == 0
    except (TypeError, ValueError):
        free = False
    spec = f"{res or '未知'} / {codec}" + (" / 无码" if uncensored else "")
    return {
        "src": "PT",
        "site": site,
        "spec": spec,
        # res/codec 单独透出（2026-11-20）：score_candidate 的分辨率策略档读它——
        # 此前只有拼好的 spec 字符串，策略档永远拿不到值，非馒头站点恒按「未知 +5」计分
        "res": res,
        "codec": codec,
        "cnsub": cnsub,
        "uncensored": uncensored,
        "size": human_size(_get(torrent, "size", 0)),
        "seeders": seeders,
        "free": free,
        "res": res,
        "pref": bool(pref_sites) and site in (pref_sites or []),
        "group": detect_group(title),
        "title": title,
        # 副标题（0.6.3 加）：馒头 `smallDescr` / 肉丝 `subtitle` / MP 索引器的 `description`
        # 都是站点发布者自己填的那一行。**任务页候选行下面显示的就是它**（原来是种子原名）：
        # 同一部片在同一站的多条记录，种名往往一字不差（实测 SONE-565 在馒头 10 条里 6 条同名），
        # 真正区分它们的是副标题（「S1美女20周年纪念版 4k x265 简体中文软字幕 + 演员清单」）。
        "sub": str(description or "").strip(),
        # 发布时间（原始格式，界面只截到日期）：老片的重发/新种靠它区分先后
        "pubdate": str(_get(torrent, "pubdate")).strip(),
        "score": 0,
        "pass": True,
        "breakdown": "",
        "picked": False,
    }


def _get(row: Any, key: str, default: Any = "") -> Any:
    """同时兼容 dict 与宿主对象（TorrentInfo）的取字段写法。"""
    if isinstance(row, dict):
        value = row.get(key)
    else:
        value = getattr(row, key, None)
    return default if value is None else value


def candidate_from_magnet(magnet: Dict[str, Any], pref_sites: Optional[List[str]] = None) -> Dict[str, Any]:
    """把 javdb 磁力记录转成 BT 兜底候选。

    注意：javdb 磁力记录的 `size` 单位是 **MB**（实测 7895 ≈ 7.9GB），不是字节，
    直接按字节格式化会得到荒唐的“8 KB”。
    """
    title = str(magnet.get("name") or "")
    uncensored = detect_uncensored(title) or bool(magnet.get("uncensored"))
    res = detect_resolution(title) or "1080p"
    spec = f"{res} / {detect_codec(title) or 'H264'}" + (" / 无码" if uncensored else "")
    try:
        size_mb = float(magnet.get("size") or 0)
    except (TypeError, ValueError):
        size_mb = 0.0
    return {
        "src": "BT",
        "site": "BT 公网",
        "spec": spec,
        "cnsub": bool(magnet.get("cnsub")) or detect_cnsub(title),
        "uncensored": uncensored,
        "size": human_size(size_mb * 1024 * 1024) if size_mb else "—",
        "seeders": int(magnet.get("seeders") or 0),
        "free": True,
        "res": res,
        "pref": False,
        "group": "BT 匿名发布",
        "title": title,
        # BT 兜底没有「发布页」，也没有站点填的副标题/发布时间：两个字段留空，
        # 界面据此不画原帖链接与副标题行（不要编造）
        "sub": "",
        "pubdate": "",
        "hash": str(magnet.get("hash") or ""),
        "score": 0,
        "pass": True,
        "breakdown": "",
        "picked": False,
    }


# ---------- 破解关键字词表（FEAT-20261120-01，2026-11-20 定稿） ----------

def clean_keywords(value: Any) -> List[str]:
    """规则里的 `crackKeywords` → 去空白、去重的词表（读侧清洗，存原样供回显）。"""
    if not isinstance(value, (list, tuple)):
        return []
    seen: set = set()
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _keyword_pattern(keyword: str) -> "re.Pattern[str]":
    """单个关键字的匹配式：纯 ASCII 词按**词边界**（`ltx` 不撞 `ULTX`/长词内部），
    中文等退回子串匹配（大小写一律不敏感）。"""
    if re.fullmatch(r"[A-Za-z0-9._-]+", keyword):
        return re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])",
            re.IGNORECASE)
    return re.compile(re.escape(keyword), re.IGNORECASE)


def detect_crack_keyword(title: Any, subtitle: Any, keywords: Any) -> str:
    """按词表探测破解版关键字；命中返回**词表里的原词**（明细里显示），未命中空串。

    - 种名与副标题都查（发布者常把模型/组 tag 写在副标题里）；
    - 词表按长度降序尝试：多个命中时报最具体的那个（`ltx模型` 压过 `ltx`）；
    - ASCII 词带词边界（误撞 ULTX 之类由它兜住），`ltxv2` 这类变体请自行加词。
    """
    words = clean_keywords(keywords)
    if not words:
        return ""
    text = f"{title or ''}\n{subtitle or ''}"
    if not text.strip():
        return ""
    for keyword in sorted(words, key=len, reverse=True):
        if _keyword_pattern(keyword).search(text):
            return keyword
    return ""


#: 词表命中加分的档位（压过泛破解 +14，低于中字 +32）
CRACK_KEYWORD_BONUS = 24


def score_candidate(cand: Dict[str, Any], rule: Dict[str, Any]) -> Dict[str, Any]:
    """按订阅规则给单个候选打 0-100 分并生成明细（数值取演示原型口径）。"""
    score = 0
    passed = True
    why: List[str] = []

    # 分辨率策略（规则里的 `res`）**参与打分**：'1080p' 优先 / '2160p' 4K 优先 / 'only4k' 仅 4K。
    # 2026-10-13 修：此前这里从不读 `rule['res']`，于是「4K 优先」与「1080P 优先」打分一模一样
    # （1080P 恒 +30、4K 恒 +16），「仅 4K」也不排除 1080P —— 选项摆着却不起作用。
    strategy = str(rule.get("res") or "1080p")
    res = cand.get("res") or ""
    if strategy == "only4k":
        if res == "2160p":
            score += 30
            why.append("4K +30（仅 4K）")
        else:
            passed = False
            why.append(f"{res or '未知'} ✗（仅 4K）")
    elif strategy == "2160p":
        if res == "2160p":
            score += 30
            why.append("4K +30（4K 优先）")
        elif res == "1080p":
            score += 16
            why.append("1080P 次选 +16")
        else:
            score += 5
            why.append(f"{res or '未知'} +5")
    elif res == "1080p":
        score += 30
        why.append("1080P +30")
    elif res == "2160p":
        # 「允许 4K 择优」只在这个策略下有意义（1080P 没有好种时才升级 4K）
        if rule.get("allow4k"):
            score += 16
            why.append("4K 择优 +16")
        else:
            why.append("4K 未开择优 +0")
    else:
        score += 5
        why.append(f"{res or '未知'} +5")

    if cand.get("cnsub"):
        score += 32
        why.append("中字 +32")
    elif rule.get("mustCnsub"):
        passed = False
        why.append("缺中字 ✗")

    # 破解/关键字分层（FEAT-20261120-01）：词表命中 +24 压过泛破解 +14；词表为空时
    # 行为与 0.9.5 完全一致。命中在种名+副标题里找，命中词写进 cand["crack_hit"]
    # 供明细与界面回显。注意「词表命中」不要求 uncensored 标志成立——LTX 版种名
    # 常不带"破解"字样，词表本身就是精准信号。
    if rule.get("preferUncensored"):
        keywords = clean_keywords(rule.get("crackKeywords"))
        hit = detect_crack_keyword(cand.get("title"), cand.get("sub"), keywords)
        if hit:
            cand["crack_hit"] = hit
        if cand.get("uncensored"):
            if hit:
                score += CRACK_KEYWORD_BONUS
                why.append(f"关键字 {hit} +{CRACK_KEYWORD_BONUS}")
            else:
                score += 14
                why.append("破解 +14")
        elif hit:
            score += CRACK_KEYWORD_BONUS
            why.append(f"关键字 {hit} +{CRACK_KEYWORD_BONUS}")
        # 硬门槛（默认关）：只「词表非空 + 优先开着」才生效，未命中直接淘汰——
        # 「我说要 ltx 就只下 ltx，没有就不下」；词表空时开它等于清空全部候选，不生效。
        if keywords and rule.get("requireCrackKeyword") and not hit:
            passed = False
            why.append("未命中关键字 ✗")

    if cand.get("src") == "PT":
        score += 8
        why.append("PT 源 +8")
    else:
        score -= 6
        why.append("BT 兜底 -6")

    if cand.get("pref"):
        score += 10
        why.append("优先站 +10")

    seeders = int(cand.get("seeders") or 0)
    min_seeders = int(rule.get("minSeeders") or 0)
    if cand.get("src") == "BT" and seeders <= 0:
        # BT 磁力不提供做种数，按“未知”处理，不能因此淘汰（否则兜底永远不成立）
        why.append("做种未知（BT 磁力）")
    elif seeders >= 50:
        score += 10
        why.append(f"做种{seeders} +10")
    elif seeders >= 10:
        score += 6
        why.append(f"做种{seeders} +6")
    elif seeders >= min_seeders:
        score += 2
        why.append(f"做种{seeders} +2")
    else:
        passed = False
        why.append(f"做种{seeders} ✗")

    if cand.get("free"):
        score += 3
        why.append("免费 +3")

    cand["score"] = max(0, min(100, score))
    cand["pass"] = passed
    cand["breakdown"] = " · ".join(why)
    return cand


def rank_candidates(cands: List[Dict[str, Any]], rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """给全部候选打分并排序：通过优先，其次按分数降序；首个通过的标记为推荐。"""
    for cand in cands:
        score_candidate(cand, rule)
    cands.sort(key=lambda c: (0 if c.get("pass") else 1, -int(c.get("score") or 0)))
    for cand in cands:
        cand["picked"] = False
    for cand in cands:
        if cand.get("pass"):
            cand["picked"] = True
            break
    return cands


def explain(task_cands: List[Dict[str, Any]], rule: Dict[str, Any]) -> str:
    """生成“为什么选它”的一句话说明（对应演示页脚文案）。"""
    best = next((c for c in task_cands if c.get("pass")), None)
    res_label = {"1080p": "1080P 优先", "2160p": "4K 优先", "only4k": "仅 4K"}.get(
        str(rule.get("res") or "1080p"), "1080P 优先"
    )
    rule_text = f"规则「{res_label}"
    if rule.get("mustCnsub"):
        rule_text += " / 必须中字"
    if rule.get("preferUncensored"):
        rule_text += " / 破解优先"
        keywords = clean_keywords(rule.get("crackKeywords"))
        if keywords:
            rule_text += f"（关键字{len(keywords)}个：{'、'.join(keywords[:3])}"
            if len(keywords) > 3:
                rule_text += "…"
            rule_text += "）"
        if rule.get("requireCrackKeyword") and keywords:
            rule_text += " / 必须命中关键字"
    rule_text += f" / 做种≥{int(rule.get('minSeeders') or 0)}」逐项打分；"
    if not best:
        return rule_text + "没有候选满足规则 → 不自动下载，等你自己判断"
    return (
        f"{rule_text}最高分 {best.get('src')} {best.get('site')} {best.get('spec')}。"
        f"明细：{best.get('breakdown')}"
    )
