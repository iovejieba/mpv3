"""LTX 库（ltxlib.dpdns.org）加密 API 客户端。

站点是 Jasna-LTX Library 的 App 式 JSON API，但**响应整体加密下发**（2026-11-20
逆向自站点前端 bundle，逐行对齐后用 Python 复刻）：

1. 握手 `POST /api/crypto/session`：客户端临时 ECDH P-256 密钥对，body
   `{"v":1,"publicKey":"<b64url 65B 非压缩点>"}`；服务端回
   `{"v":1,"sid","expiresAt","publicKey"(65B),"salt"(32B)}`；
2. 密钥：ECDH 共享 256bit → HKDF-SHA-256（salt、info=`ltx-api-response-v1:{sid}`）
   → AES-256-GCM；
3. 数据请求带 `X-LTX-Session`（sid）与 `X-LTX-Request`（每请求随机 16B b64url）；
4. 响应 content-type 为 `application/vnd.ltx.encrypted+json`，信封
   `{"v":1,"sid","rid","iv"(12B),"body","encoding":"identity"|"gzip"}`，
   **AAD = ["ltx-api-response-v1", sid, rid, METHOD, path+query, HTTP状态码, encoding]**
   （JSON 数组紧凑序列化，顺序固定）；`428` + `reason=cryptoSession` 时重握手再试一次；
5. 未加密的 JSON 错误体形如 `{"error","reason",…}`：`reason="verify"` 是源站
   Turnstile 人机闸（浏览器才能过，自动流程到此为止、如实上报）。

这是防爬混淆而非鉴权——密钥全部公开可推导，但协议在演进（信封 v1）：任何解析
失败都要落到闭枚举 reason 上给界面看，**禁止吞成未知错误**（0.8.1 的纪律）。

`cryptography` 缺失时整体降级：`HAVE_CRYPTO=False`，`handshake()` 抛
`kind="crypto"` 的 LtxError，上层把原因摆到设置台。
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------- 加密依赖（缺失不炸，运行时报原因） ----------

try:
    from cryptography.hazmat.primitives.asymmetric.ec import (
        ECDH,
        SECP256R1,
        generate_private_key,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - 无宿主依赖的环境才走到
    HAVE_CRYPTO = False

API_TAG = "ltx-api-response-v1"
ENC_CONTENT_TYPE = "application/vnd.ltx.encrypted+json"

#: 官方库地址（设置台 `ltxSite` 可覆盖；换镜像站时只改这一处默认值）
DEFAULT_SITE = "https://ltxlib.dpdns.org"

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# CF 边缘拦截面（复用 0.8.0 摸出来的强特征口径，这里只认「这肯定被 CF 拦了」）
_CF_MARKERS = ("just a moment", "attention required", "_cf_chl_opt",
               "cf-browser-verification", "checking your browser")

#: 信封校验里允许的 encoding（与前端一致，别的值直接判坏信封）
_ENCODINGS = ("identity", "gzip")

Fetcher = Callable[[str, str, Dict[str, str], Optional[bytes]],
                   Tuple[int, str, bytes, Dict[str, str]]]
#: fetcher 返回 (HTTP状态码, Content-Type, 响应体, 本轮 Set-Cookie 的 {名: 值})

# ---------- 错误（闭枚举 reason，界面按 reason 分组） ----------

#: 全部合法取值（与 scrape 链的 REASONS 同风格，只加不改）
REASONS = ("verify", "cf", "session", "http", "parse", "network", "crypto")


class LtxError(Exception):
    """LTX 客户端失败；verify 时带 sitekey 供界面展示。

    `kind`：本端分类走 `REASONS` 闭枚举；**服务端 error JSON 里自报的 reason 原样
    透传**（协议在演进，把新 reason 强并进旧枚举等于撒谎——界面按 kind 分组，
    未知组显示原文比「未知错误」有用得多）。
    """

    def __init__(self, kind: str, message: str, sitekey: str = ""):
        super().__init__(message)
        self.kind = str(kind or "http")
        self.sitekey = sitekey


# ---------- b64url（前端口径：无填充） ----------

def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(str(text) + pad)


def _aad(sid: str, rid: str, method: str, path: str, status: int,
         encoding: str) -> bytes:
    """AAD 的 JSON 数组序列——与前端 `JSON.stringify([tag, sid, rid, …])` 逐字节一致。"""
    return json.dumps([API_TAG, sid, rid, str(method).upper(), path, int(status),
                       encoding], separators=(",", ":")).encode("utf-8")


def _hkdf_info(sid: str) -> bytes:
    return f"{API_TAG}:{sid}".encode("utf-8")


# ---------- 默认 HTTP 层：curl_cffi（Chrome 指纹）优先，缺则 urllib ----------

def _looks_like_cf(body: bytes) -> bool:
    low = body[:4096].decode("utf-8", "replace").lower()
    return any(marker in low for marker in _CF_MARKERS)


def _make_fetcher(timeout: int = 45) -> Tuple[Fetcher, str]:
    """构造默认 fetcher，返回 (fetcher, 层名)。

    优先 `curl_cffi`（Chrome TLS 指纹，CF 对纯 Python TLS 常给挑战页）；
    库缺失回落 urllib（真实可用性取决于出口 IP 信誉——MP 容器里以探针实测为准）。
    UA 由**调用方**放在 headers 里传进来（过盾收割后要切换 UA），fetcher 不掺和；
    两层都会把响应的 Set-Cookie 汇成 {名: 值} 交回（调用方存进 cookie 罐）。
    """
    try:
        from curl_cffi import requests as _creq

        def fetch_curl(method: str, url: str, headers: Dict[str, str],
                       body: Optional[bytes]) -> Tuple[int, str, bytes, Dict[str, str]]:
            resp = _creq.request(method.upper(), url, headers=headers, data=body,
                                 impersonate="chrome", timeout=timeout)
            cookies = {str(k): str(v) for k, v in dict(resp.cookies or {}).items()}
            return (int(resp.status_code), str(resp.headers.get("Content-Type", "")),
                    resp.content, cookies)

        return fetch_curl, "curl_cffi"
    except ImportError:
        pass

    import urllib.error
    import urllib.request

    def _parse_set_cookie(raw: str) -> Optional[Tuple[str, str]]:
        first = raw.split(";", 1)[0].strip()
        if "=" not in first:
            return None
        name, _, value = first.partition("=")
        return name.strip(), value.strip()

    def fetch_urllib(method: str, url: str, headers: Dict[str, str],
                     body: Optional[bytes]) -> Tuple[int, str, bytes, Dict[str, str]]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            resp_ctx = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            resp_ctx = error
        with resp_ctx:
            raw = resp_ctx.headers.get_all("Set-Cookie") or []
            cookies: Dict[str, str] = {}
            for line in raw:
                parsed = _parse_set_cookie(line)
                if parsed:
                    cookies[parsed[0]] = parsed[1]
            return (int(resp_ctx.status),
                    str(resp_ctx.headers.get("Content-Type", "")),
                    resp_ctx.read(), cookies)

    return fetch_urllib, "urllib"


def _classify_status(status: int, ct: str, body: bytes) -> LtxError:
    """非 2xx / 非加密响应 → 闭枚举失败（verify 的 sitekey 原样带出）。"""
    low_ct = (ct or "").split(";")[0].strip().lower()
    if low_ct == "application/json" or body[:1] in (b"{", b"["):
        try:
            data = json.loads(body.decode("utf-8", "replace"))
            if isinstance(data, dict) and (data.get("error") or data.get("reason")):
                reason = str(data.get("reason") or "http")
                message = str(data.get("error") or reason)
                return LtxError(reason, message, str(data.get("sitekey") or ""))
        except (ValueError, UnicodeDecodeError):
            pass
    if status in (403, 503) or _looks_like_cf(body):
        return LtxError("cf", f"Cloudflare 拦截（HTTP {status}）")
    return LtxError("http", f"HTTP {status}（{low_ct or '未知类型'}）")


class LtxClient:
    """一次持有一个加密会话；线程安全（握手加锁，重爬任务串行调用）。"""

    def __init__(self, base_url: str, timeout: int = 45, ua: str = "",
                 fetcher: Optional[Fetcher] = None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.timeout = timeout
        self._fetcher = fetcher
        self._fetcher_layer = "injected" if fetcher is not None else ""
        if self._fetcher is None:
            self._fetcher, self._fetcher_layer = _make_fetcher(timeout)
        self._ua = ua or DEFAULT_UA
        self._session: Optional[Dict[str, Any]] = None  # {sid, key, expires_at}
        self._cookies: Dict[str, str] = {}               # 站点种的标记（ltx_c 等），带回即可
        self._lock = threading.Lock()

    def ingest_cookies(self, cookies: List[Dict[str, str]], ua: str = "") -> None:
        """过盾收割的 cookies（与 UA 配套）注入会话层——CF 的信任状态常绑 UA+IP。"""
        with self._lock:
            for item in cookies or []:
                if item.get("name"):
                    self._cookies[str(item["name"])] = str(item.get("value") or "")
        if ua:
            self._ua = ua

    @property
    def fetcher_layer(self) -> str:
        """实际使用的 HTTP 层名（curl_cffi / urllib / injected），日志与诊断用。"""
        return self._fetcher_layer

    def ready(self) -> bool:
        text = str(self.base_url or "").strip()
        return text.startswith(("http://", "https://")) and len(text) > 8

    # ---------- 会话 ----------

    def handshake(self) -> None:
        """ECDH 握手建会话（每次新密钥对，与前端一致）。"""
        if not HAVE_CRYPTO:
            raise LtxError("crypto", "容器缺少 cryptography 库，无法与 LTX 库建立加密会话")
        if not self.ready():
            raise LtxError("network", "LTX 库地址未配置")
        priv = generate_private_key(SECP256R1())
        pub_raw = priv.public_key().public_bytes(Encoding.X962,
                                                 PublicFormat.UncompressedPoint)
        body = json.dumps({"v": 1, "publicKey": b64u(pub_raw)},
                          separators=(",", ":")).encode("utf-8")
        status, ct, payload, cookies = self._fetcher(
            "POST", self.base_url + "/api/crypto/session",
            {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": self._ua,
             # 源站有「网页操作」检查：缺 Origin/Referer 直接 reason:"origin" 拒掉
             "Origin": self.base_url, "Referer": self.base_url + "/"}, body)
        if cookies:
            with self._lock:
                self._cookies.update(cookies)
        if status != 200:
            raise _classify_status(status, ct, payload)
        try:
            session = json.loads(payload.decode("utf-8"))
            sid = str(session["sid"])
            expires_at = float(session["expiresAt"])
            server_pub = b64u_decode(session["publicKey"])
            salt = b64u_decode(session["salt"])
        except (ValueError, KeyError, TypeError) as error:
            raise LtxError("parse", f"会话响应异常：{error}") from error
        if session.get("v") != 1 or not sid or expires_at <= time.time() * 1000:
            raise LtxError("parse", "会话响应异常：v/sid/expiresAt 不合法")
        if len(server_pub) != 65 or len(salt) != 32:
            raise LtxError("parse",
                           f"会话响应异常：公钥 {len(server_pub)}B / salt {len(salt)}B")
        try:
            from cryptography.hazmat.primitives.asymmetric.ec import (
                EllipticCurvePublicKey,
            )

            shared = priv.exchange(
                ECDH(), EllipticCurvePublicKey.from_encoded_point(SECP256R1(), server_pub))
            key = HKDF(algorithm=SHA256(), length=32, salt=salt,
                       info=_hkdf_info(sid)).derive(shared)
        except Exception as error:
            raise LtxError("crypto", f"密钥推导失败：{error}") from error
        with self._lock:
            self._session = {"sid": sid, "key": key, "expires_at": expires_at}

    def _ensure_session(self) -> Dict[str, Any]:
        """会话复用到 expiresAt（前端口径），过期重握手。"""
        with self._lock:
            current = self._session
        if current and current["expires_at"] > time.time() * 1000 + 10_000:
            return current
        self.handshake()
        with self._lock:
            return self._session or {}

    def invalidate(self) -> None:
        with self._lock:
            self._session = None

    # ---------- 加密数据请求 ----------

    def request(self, method: str, path: str, query: str = "",
                body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        """一条数据请求：解密后返回 (HTTP状态码, JSON)。

        428/会话失效自动重握手一次（与前端 2 次循环一致）；非加密响应按
        `_classify_status` 分类抛 `LtxError`。**409 等业务状态原样返回**给调用方
        （快照过期就是这么感知的）。
        """
        full_path = path + (f"?{query.lstrip('?')}" if query else "")
        payload_bytes: Optional[bytes] = None
        if body is not None:
            payload_bytes = json.dumps(body, separators=(",", ":"),
                                       ensure_ascii=False).encode("utf-8")
        last_error: Optional[LtxError] = None
        for attempt in range(2):
            session = self._ensure_session()
            rid = b64u(os.urandom(16))
            headers = {"Accept": "application/json",
                       "User-Agent": self._ua,
                       "Origin": self.base_url, "Referer": self.base_url + "/",
                       "X-LTX-Session": session["sid"],
                       "X-LTX-Request": rid}
            with self._lock:
                if self._cookies:
                    headers["Cookie"] = "; ".join(
                        f"{k}={v}" for k, v in self._cookies.items())
            try:
                status, ct, payload, cookies = self._fetcher(
                    method, self.base_url + full_path, headers, payload_bytes)
            except LtxError:
                raise
            except Exception as error:
                raise LtxError("network",
                               f"{type(error).__name__}: {error}") from error
            if cookies:
                with self._lock:
                    self._cookies.update(cookies)
            ct_low = (ct or "").split(";")[0].strip().lower()
            if status == 428 and attempt == 0 and ct_low == "application/json":
                try:
                    err = json.loads(payload.decode("utf-8", "replace"))
                    if str(err.get("reason") or "") == "cryptoSession":
                        self.invalidate()
                        continue            # 重握手后重试（仅一次）
                except ValueError:
                    pass
            if ct_low == ENC_CONTENT_TYPE or (
                    status == 200 and payload[:1] == b"{"
                    and b'"sid"' in payload[:200] and b'"rid"' in payload[:200]
                    and b'"iv"' in payload[:200]):
                data = self._decrypt(payload, session, rid, method, full_path, status)
                return status, data
            if status >= 400 or ct_low not in ("application/json",):
                raise _classify_status(status, ct, payload)
            try:
                data = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as error:
                raise LtxError("parse", f"响应不是 JSON：{error}") from error
            if isinstance(data, dict) and (data.get("error") or data.get("reason")):
                last_error = LtxError(str(data.get("reason") or "http"),
                                      str(data.get("error") or "请求被拒"),
                                      str(data.get("sitekey") or ""))
                if str(data.get("reason") or "") == "cryptoSession" and attempt == 0:
                    self.invalidate()
                    continue
                raise last_error
            return status, data
        raise last_error or LtxError("session", "会话重试耗尽")

    def _decrypt(self, payload: bytes, session: Dict[str, Any], rid: str,
                 method: str, path: str, status: int) -> Any:
        """按信封规范解密；任何偏差落 `parse`（协议升级时用户能看到准确原因）。"""
        if not HAVE_CRYPTO:
            raise LtxError("crypto", "缺少 cryptography 库，无法解密响应")
        try:
            envelope = json.loads(payload.decode("utf-8"))
            if envelope.get("v") != 1:
                raise ValueError("信封 v 不是 1（协议可能已升级）")
            if envelope.get("sid") != session["sid"]:
                raise ValueError("信封 sid 与会话不符")
            if envelope.get("rid") != rid:
                raise ValueError("信封 rid 与请求不符")
            encoding = envelope.get("encoding")
            if encoding not in _ENCODINGS:
                raise ValueError(f"encoding={encoding!r} 不合法")
            iv = b64u_decode(envelope["iv"])
            if len(iv) != 12:
                raise ValueError(f"iv {len(iv)}B 不合法")
            aad = _aad(session["sid"], rid, method, path, status, encoding)
            plain = AESGCM(session["key"]).decrypt(iv, b64u_decode(envelope["body"]), aad)
            if encoding == "gzip":
                plain = gzip.decompress(plain)
            return json.loads(plain.decode("utf-8"))
        except LtxError:
            raise
        except Exception as error:
            raise LtxError("parse", f"解密失败：{error}") from error

    # ---------- 业务端点 ----------

    def catalogue(self) -> Any:
        """全量目录（含 snapshot 版本号）。"""
        return self.request("GET", "/api/items")[1]

    def query(self, **params: Any) -> Any:
        """目录检索（q/actor/genres/type/status/offset/limit/metadata/…）。"""
        return self.request("POST", "/api/items/query", body=params)[1]


def extract_links(items: List[Dict[str, Any]],
                  kind: str = "magnet") -> List[Dict[str, Any]]:
    """从目录条目里取「已收录且有链接」的条目；kind 过滤 ed2k/magnet。

    字段名来自前端卡片渲染（`item.link` 链接原串、`item.url` 详情页、
    `item.status` ok/pending、`item.type` ed2k|magnet）。ed2k 只有 115 离线
    能用（qB/aria2 都不支持），默认只收 magnet。
    """
    picked: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        if not link or str(item.get("status") or "ok") != "ok":
            continue
        if str(item.get("type") or "").lower() != kind:
            continue
        if kind == "magnet" and not link.startswith("magnet:"):
            continue
        picked.append(item)
    return picked
