# -*- coding: utf-8 -*-
"""免费到期未完成处置（9.12.0 测试）：解析与目标筛选，纯函数、无宿主依赖。

背景与设计契约（探讨定稿 2026-10-26）：
- 免费种子过期后，未下完部分的下载量会被站点照常计费（NexusPHP 按 announce 时点
  的促销状态结算）；本模块为"到期未完成 → 暂停/删除"提供解析与筛选。
- deadline 双格式：站点促销图标的 title 属性可能是绝对时间（"免费：2026-10-27
  23:40:22"）也可能是倒计时（"23小时45分钟"）。倒计时只在抓取时刻有效——必须
  在记录创建时换算成绝对时刻，绝不在扫描时回溯解析（会虚增 deadline）。
- 数据许可原则：解析不出来（无期限/坏文本）= 该种子不参与处置，静默跳过，绝不猜。
- 全站 H&R 站与策略线（hr_policy）不适用此功能（未完成即删有 H&R 风险），由调用方把门。
"""

import datetime
import re
import time
from typing import Any, Dict, Optional

# 到期缓冲：暂停/删除在 deadline 前该秒数触发。站点按 announce 时点结算，
# 最后一个 announce 间隔内的免费字节会在过期后的 announce 上被计费——
# 提前一个 announce 间隔（30 分钟保守值）动手，宁少下 30 分钟免费，不冒计费风险。
FREE_EXPIRY_BUFFER_SECONDS = 30 * 60

# 展示警示线：剩余低于该秒数时倒计时升级 warning 色（红色留给"已到期未处置"）
FREE_EXPIRY_WARN_SECONDS = 2 * 3600

# 处置动作
FREE_EXPIRY_ACTIONS = ("off", "pause", "delete")

# 绝对时间（可安全地在任意时刻回溯解析——deadline 是固定时刻）
_ABSOLUTE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[\sT](\d{1,2}):(\d{2})(?::(\d{2}))?")
# 倒计时（只在抓取时刻有效）
_COUNTDOWN_DAYS_RE = re.compile(r"(\d+)\s*天")
_COUNTDOWN_HOURS_RE = re.compile(r"(\d+)\s*(?:小时|时(?!钟))")
_COUNTDOWN_MINUTES_RE = re.compile(r"(\d+)\s*(?:分钟|分)")


def parse_free_deadline(
    text: Any, *, now: Optional[float] = None, strict_absolute: bool = False
) -> Optional[float]:
    """把站点促销截止文本解析为 epoch 秒；解析失败返回 None（fail-open，不猜）。

    - 绝对时间优先："免费：2026-10-27 23:40:22" / "2026-10-27T23:40" 等；
    - 倒计时兜底："23小时45分钟" / "1天2小时" / "45分钟"——以 now 为锚换算，
      因此只允许在抓取时刻调用；
    - strict_absolute=True 时拒绝倒计时（供历史记录扫描期回溯解析使用，
      防止把几天前的倒计时按当前时刻平移出虚假 deadline）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    now_ts = float(now if now is not None else time.time())
    match = _ABSOLUTE_RE.search(raw)
    if match:
        year, month, day, hour, minute = (int(match.group(i)) for i in range(1, 6))
        second = int(match.group(6) or 0)
        if not (2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31
                and hour <= 23 and minute <= 59 and second <= 59):
            return None
        try:
            # naive datetime 的 .timestamp() 按本地时区解释——与站点展示时刻同源
            moment = datetime.datetime(year, month, day, hour, minute, second)
            return moment.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if strict_absolute:
        return None
    days = _COUNTDOWN_DAYS_RE.search(raw)
    hours = _COUNTDOWN_HOURS_RE.search(raw)
    minutes = _COUNTDOWN_MINUTES_RE.search(raw)
    if not (days or hours or minutes):
        return None
    total = (
        (int(days.group(1)) * 86400 if days else 0)
        + (int(hours.group(1)) * 3600 if hours else 0)
        + (int(minutes.group(1)) * 60 if minutes else 0)
    )
    if total <= 0:
        return None
    return now_ts + total


def _is_incomplete(record: Dict[str, Any], info: Dict[str, Any]) -> bool:
    """未完成判据：qB 快照 progress 优先，缺失时回退记录 downloaded/size 对比。"""
    progress = info.get("progress")
    if progress is not None:
        try:
            return float(progress) < 1
        except (TypeError, ValueError):
            pass
    size = float(record.get("size") or 0)
    downloaded = float(record.get("downloaded") or 0)
    return bool(size > 0 and downloaded < size)


def select_free_expiry_targets(
    records: Dict[str, Dict[str, Any]],
    infos: Dict[str, Dict[str, Any]],
    *,
    now: float,
    buffer_seconds: int = FREE_EXPIRY_BUFFER_SECONDS,
) -> Dict[str, Dict[str, Any]]:
    """筛选到期未完成的处置目标。

    - records: 托管记录（free_until / freedate / deleted / free_paused / title / size）；
    - infos: 本轮下载器快照（progress / state），键为 hash；快照缺失的种子
      （已从下载器消失）不处置——交给缺册自愈逻辑定性；
    - 返回 {hash: 目标信息}，包含 deadline 与触发原因。
    """
    targets: Dict[str, Dict[str, Any]] = {}
    for torrent_hash, record in records.items():
        if not isinstance(record, dict) or record.get("deleted") or record.get("free_paused"):
            continue
        if record.get("hit_and_run"):
            # 单颗 H&R 标记种子永不处置（9.13.0-beta.4 兼容缺口）：未完成即删/暂停
            # 对带 H&R 义务的种子有记账风险，fail-closed——全站 HR 站已在调用方排除，
            # 这里补单颗标记的漏网（选种过滤/收编种子可能带入）。
            continue
        if torrent_hash not in infos:
            # 本轮下载器快照缺失（种子已消失）——不处置，交给缺册自愈逻辑定性
            continue
        info = infos[torrent_hash]
        if not _is_incomplete(record, info):
            continue
        # 用户手动暂停的种子 = 用户接管信号，sweep 让位（9.13.0-beta.4 修订）：
        # "你的手最高"是宪法级排序——否则 delete 档会删掉用户刻意暂停留待后续的种子。
        # 顺带让 pause 档不对已暂停种子打无效 stop。
        info_state = str(info.get("state") or "").lower()
        if info.get("is_paused") or info_state.startswith(("paused", "stopped")):
            continue
        # free_until 是抓取时刻解析的权威值；缺失时仅允许绝对时间回溯（历史记录）
        deadline = record.get("free_until")
        if deadline is None:
            deadline = parse_free_deadline(
                record.get("freedate"), now=now, strict_absolute=True)
        try:
            deadline = float(deadline) if deadline is not None else None
        except (TypeError, ValueError):
            deadline = None
        if deadline is None:
            continue
        if deadline - now > buffer_seconds:
            continue
        targets[torrent_hash] = {
            "title": record.get("title") or torrent_hash,
            "deadline": deadline,
            "expired": deadline <= now,
            "within_buffer": deadline > now,
        }
    return targets


def format_remaining_cn(seconds: float) -> str:
    """剩余秒数 → 中文时长（"1天3小时" / "2小时45分钟" / "45分钟"），0 秒兜底 1 分钟。"""
    seconds = max(int(seconds), 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{max(minutes, 1)}分钟"


def free_countdown_state(
    record: Any, *, now: float, warn_seconds: int = FREE_EXPIRY_WARN_SECONDS
) -> Optional[Dict[str, str]]:
    """种子展示态（9.12.0）：免费倒计时四级色阶，None = 不显示。

    语义是"该用户做什么"而不是"发生了什么"：
    - plain   免费剩 1天3小时（充裕，纯信息）
    - warning 免费剩 45分钟（临近警示线）
    - error   免费已到期（卡在临界态，sweep 下一轮才会动它——红色稀缺才有效）
    - info    已暂停 · 免费到期（处置完成态，安全，不用红色）
    已删除记录返回 None（已有既有展示，不重复）。倒计时按本地时刻现算，
    前端另有同语义 JS 版（记录载荷自带 free_until）。
    """
    if not isinstance(record, dict) or record.get("deleted"):
        return None
    # 已完成种子不显示倒计时（9.13.0-beta.4 修订）：计费已成定局，"到期"失去意义，
    # 继续显示会让完成种子挂着红色"免费已到期"误导（sweep 本就只处置未完成）。
    size = float(record.get("size") or 0)
    downloaded = float(record.get("downloaded") or 0)
    if size > 0 and downloaded >= size:
        return None
    if record.get("free_paused"):
        return {"text": "已暂停 · 免费到期", "level": "info"}
    deadline = record.get("free_until")
    try:
        deadline = float(deadline) if deadline is not None else None
    except (TypeError, ValueError):
        deadline = None
    if deadline is None:
        return None
    remaining = deadline - float(now)
    if remaining <= 0:
        return {"text": "免费已到期", "level": "error"}
    text = f"免费剩 {format_remaining_cn(remaining)}"
    if remaining <= warn_seconds:
        return {"text": text, "level": "warning"}
    return {"text": text, "level": "plain"}
