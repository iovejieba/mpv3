"""FlareSolverr 客户端：过 Cloudflare 盾拿「可直连」的 cookies + UA。

**会话生命周期是本模块的第一要务**（2026-11-20 定的纪律）：FlareSolverr 每个命名
session 在它自己进程里养着一只真浏览器（几百 MB 常驻），调了不销毁就是内存泄漏——
用户明确要求「处理好每次调用的收尾结束，防止 fl 内存残留爆炸」。所以：

1. **单次过盾**（`solve()`）不带 session：FlareSolverr 用完即弃的临时浏览器，自己收尾；
2. **需要复用浏览器**的场景一律走 `browser_session()` 上下文管理器：`finally` 里
   无条件 `sessions.destroy`，异常也不留尾巴；
3. **兜底清扫**（`sweep()`）：按 `avhelper-` 前缀把上一轮进程崩溃留下的僵尸 session
   全部销毁——`init_plugin` 每次启动都调一次。FlareSolverr 对同名 session 重复
   create 会报错，所以先扫再建是硬前提，不只是卫生习惯；
4. `maxTimeout` 压短（默认 60s）：页面挂死时让 FlareSolverr 自己杀浏览器，
   不把内存占用拖长。

所有输出不落任何敏感信息（FlareSolverr 不涉及凭据；URL 过 `client.mask` 同一纪律）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

# 会话名前缀：清扫只认自家前缀，不去碰用户其它工具（_bookmark 常驻包之类）的 session
SESSION_PREFIX = "avhelper-"
DEFAULT_MAX_TIMEOUT = 60_000


class FlareError(Exception):
    """FlareSolverr 调用失败。`kind`：network / status / parse / rejected。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _mask_url(url: str) -> str:
    """URL 里的 `user:pass@` 抹掉（与 client.mask 同一纪律，虽然这里通常没有）。"""
    import re

    return re.sub(r"://[^/@\s]+@", "://***@", str(url or ""))


class FlareSolverr:
    """FlareSolverr 的最小客户端：v1 API 的 solve / sessions 三件套。"""

    def __init__(self, base_url: str, timeout: int = 120,
                 max_timeout_ms: int = DEFAULT_MAX_TIMEOUT):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.timeout = timeout
        self.max_timeout_ms = max_timeout_ms

    def ready(self) -> bool:
        """地址已配置且形似 http(s) URL（不探活，探活用 `health()`）。"""
        text = str(self.base_url or "").strip()
        if not text:
            return False
        parts = urlsplit(text)
        return parts.scheme in ("http", "https") and bool(parts.netloc)

    # ---------- 底层 ----------

    def _call(self, cmd: str, **params: Any) -> Dict[str, Any]:
        """POST /v1 一条命令；非 ok 一律抛 `FlareError`（kind 已分类）。"""
        if not self.ready():
            raise FlareError("network", "FlareSolverr 地址未配置或不合法")
        body = json.dumps({"cmd": cmd, "maxTimeout": self.max_timeout_ms,
                           **params}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/v1", data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise FlareError("status",
                             f"FlareSolverr HTTP {error.code}：{detail}") from error
        except (urllib.error.URLError, OSError) as error:
            raise FlareError("network", f"FlareSolverr 连不上：{error}") from error
        except json.JSONDecodeError as error:
            raise FlareError("parse", f"FlareSolverr 返回不是 JSON：{error}") from error
        except Exception as error:
            raise FlareError("network",
                             f"FlareSolverr 调用异常 {type(error).__name__}：{error}") from error
        if not isinstance(data, dict) or data.get("status") != "ok":
            message = str((data or {}).get("message") or "未知错误")
            # 同名 session 重复 create / destroy 不存在的 session 都会走到这里
            raise FlareError("rejected", f"FlareSolverr 拒绝 {cmd}：{message}")
        elapsed = time.time() - started
        return {"data": data, "elapsed": elapsed}

    # ---------- 单次过盾（临时浏览器，FlareSolverr 自己收尾） ----------

    def solve(self, url: str) -> Dict[str, Any]:
        """`request.get` 过盾抓一个 URL；返回 {status, url, userAgent, cookies, body}。

        cookies 是 FlareSolverr 浏览器上下文里的全部 cookie（cf_clearance / 站点
        自设的标记都含），拿去配直连请求的 Cookie 头 + 同一 UA。
        """
        result = self._call("request.get", url=url)
        solution = (result["data"].get("data") or {}).get("solution") or {}
        cookies = [{"name": str(c.get("name")), "value": str(c.get("value", ""))}
                   for c in (solution.get("cookies") or []) if c.get("name")]
        return {
            "status": solution.get("status"),
            "url": solution.get("url") or url,
            "userAgent": solution.get("userAgent") or "",
            "cookies": cookies,
            "body": str(solution.get("response") or ""),
        }

    def cookie_header(self, cookies: List[Dict[str, str]]) -> str:
        """把 solve() 的 cookies 拼成请求头值。"""
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies or [])

    # ---------- 命名 session（必须走 browser_session 上下文） ----------

    def create_session(self, name: str) -> None:
        self._call("sessions.create", session=name)

    def destroy_session(self, name: str) -> None:
        self._call("sessions.destroy", session=name)

    def list_sessions(self) -> List[str]:
        """FlareSolverr 当前持有的全部 session 名（清扫用）。"""
        result = self._call("sessions.list")
        sessions = (result["data"].get("data") or {}).get("sessions") or []
        names = []
        for item in sessions:
            name = item.get("id") or item.get("session") if isinstance(item, dict) else str(item)
            if name:
                names.append(str(name))
        return names

    def sweep(self, prefix: str = SESSION_PREFIX) -> int:
        """销毁本插件前缀的全部遗留 session，返回销毁数。

        单个销毁失败不中断（下一个还要销）； FlareSolverr 整体不可达时返回 0。
        """
        try:
            names = [n for n in self.list_sessions() if str(n).startswith(prefix)]
        except FlareError:
            return 0
        destroyed = 0
        for name in names:
            try:
                self.destroy_session(name)
                destroyed += 1
            except FlareError:
                pass
        return destroyed

    def solve_in_session(self, url: str, name: str) -> Dict[str, Any]:
        """在**已存在**的命名 session 里抓一个 URL（浏览器复用，不再拉新实例）。"""
        result = self._call("request.get", url=url, session=name)
        solution = (result["data"].get("data") or {}).get("solution") or {}
        cookies = [{"name": str(c.get("name")), "value": str(c.get("value", ""))}
                   for c in (solution.get("cookies") or []) if c.get("name")]
        return {
            "status": solution.get("status"),
            "url": solution.get("url") or url,
            "userAgent": solution.get("userAgent") or "",
            "cookies": cookies,
            "body": str(solution.get("response") or ""),
        }

    # ---------- 复用浏览器的受控区（收尾由 finally 保证） ----------

    def browser_session(self, name: str):
        """命名 session 的受控生命周期：进入创建、退出**无条件**销毁。

        需要在同一浏览器实例里连续抓多个页面的场景才用它；单次抓取一律走
        `solve()`（临时浏览器）。destroy 失败只记日志不再抛——上下文退出必须
        安静完成，把异常留给 with 体。
        """
        return _FlareSession(self, name)


class _FlareSession:
    """`browser_session()` 的实现：`with solver.browser_session("avhelper-x"):`。"""

    def __init__(self, solver: FlareSolverr, name: str):
        self.solver = solver
        self.name = f"{SESSION_PREFIX}{name}" if not name.startswith(SESSION_PREFIX) else name

    def __enter__(self) -> "_FlareSession":
        self.solver.create_session(self.name)
        return self

    def solve(self, url: str) -> Dict[str, Any]:
        return self.solver.solve_in_session(url, self.name)

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.solver.destroy_session(self.name)
        except FlareError as error:
            from app.sdk.logging import logger
            logger.warning(f"AVHelper FlareSolverr session 销毁失败（收尾继续）："
                           f"{_mask_url(self.name)}；原因：{error}")
        return False      # 不吞异常：with 体的错误照常向上抛
