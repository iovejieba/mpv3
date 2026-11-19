"""LTX 库本地快照：全量目录落 JSON，运行时**零 API**。

设计（2026-11-20 定稿）：每日重爬一次全量目录（`GET /api/items`），存进
`meta/ltxlib.json`；之后浏览/检索/按番号查链接全部走本地——订阅检查匹配本地库
不发一个请求，新增条目靠前后两次 codes 集合 diff 感知。

存储形态刻意保守：**条目原样保留**（字段名以站点实测为准，protocol 还在演进，
别在落库时做有损投影）；统计与 diff 现算，量级几千条、几 MB，JSON 够用不上 SQLite。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import paths


def store_path() -> Path:
    """快照文件路径（数据根 meta/ 下，与其它 JSON 缓存同目录）。"""
    return paths.meta_dir() / "ltxlib.json"


def load() -> Optional[Dict[str, Any]]:
    """读快照；缺文件/坏 JSON 返回 None（调用方按「还没有数据」处理）。"""
    path = store_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None
    except (OSError, ValueError):
        return None


def save(data: Dict[str, Any]) -> None:
    """原子写快照（tmp + rename，并发刮削不会留下半截文件）。"""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def codes(data: Optional[Dict[str, Any]]) -> Set[str]:
    """快照里全部番号（大小写归一：站点口径不清，本插件一律小写比较）。"""
    return {str((it or {}).get("code") or "").strip().lower()
            for it in ((data or {}).get("items") or []) if (it or {}).get("code")}


def diff(old: Optional[Dict[str, Any]],
         new: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """前后两轮的番号集合 diff；`added_with_link` 才是用户真正关心的（pending 没链接）。"""
    old_codes = codes(old)
    new_codes = codes(new)
    added = sorted(new_codes - old_codes)
    removed = sorted(old_codes - new_codes)
    new_by_code = {str((it or {}).get("code") or "").strip().lower(): it
                   for it in ((new or {}).get("items") or []) if (it or {}).get("code")}
    added_with_link = sum(
        1 for code in added
        if str((new_by_code.get(code) or {}).get("link") or "").strip())
    return {"added": added, "removed": removed,
            "added_with_link": added_with_link}


def stats(data: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """条目统计：总数 / 已收录 / 待确认 / 磁力 / ed2k。"""
    items = (data or {}).get("items") or []
    ok = pending = magnet = ed2k = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        status = str(it.get("status") or "ok")
        if status == "pending":
            pending += 1
        else:
            ok += 1
        kind = str(it.get("type") or "").lower()
        if kind == "magnet" and str(it.get("link") or "").startswith("magnet:"):
            magnet += 1
        elif kind == "ed2k" and str(it.get("link") or "").startswith("ed2k://"):
            ed2k += 1
    return {"total": len(items), "ok": ok, "pending": pending,
            "magnet": magnet, "ed2k": ed2k}


def lookup(number: str, data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """按番号查本地库：只回「已收录 + magnet 链接」的条目（ed2k 丢弃，qB/aria2 不支持）。

    :param number: 番号（大小写不敏感；与站点 `code` 字段精确匹配）
    :param data: 快照；缺省现读（热路径由调用方传入避免重复 IO）
    """
    key = str(number or "").strip().lower()
    if not key:
        return []
    items = (data or {}).get("items") or []
    return [
        it for it in items
        if isinstance(it, dict)
        and str(it.get("code") or "").strip().lower() == key
        and str(it.get("status") or "ok") == "ok"
        and str(it.get("type") or "").lower() == "magnet"
        and str(it.get("link") or "").startswith("magnet:")
    ]


def status_summary(now: Optional[float] = None) -> Dict[str, Any]:
    """给界面/接口的快照状态（没有数据时也如实返回空态，不编造）。"""
    data = load()
    if not data:
        return {"ready": False, "stats": {}, "fetchedAt": "", "snapshot": ""}
    fetched_at = float(data.get("fetchedAt") or 0)
    return {
        "ready": True,
        "stats": stats(data),
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M", time.localtime(fetched_at))
        if fetched_at else "",
        "snapshot": str(data.get("snapshot") or "")[:32],
    }
