# -*- coding: utf-8 -*-
"""
user.sites.v3.bin 自维护（方案一：自动解码 + overlay 插入 + 版本管理）

职责: 让 exoticaz 的声明式定义"寄居"在官方站点资源里且免手工维护。
  - 启动自检 + 每日服务: 读已装 bin → 检查 overlay 是否在位且与当前官方版本一致
  - 需要时: 拉取官方 bin（镜像链）→ 解密 → 合并 overlay → version = 官方+1 → 原子写回
  - 失败安全: 任何一步异常都不触碰已装 bin；key 轮换时自动子进程探针抓新 key

设计依据: ../DESIGN.md §8；与 resources.v3/merge_custom_sites.py 的 CLI 手动路径语义一致。
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

from .overlay_data import OVERLAY_SITES

# 官方资源仓库（raw + 镜像，依次尝试）
RAW_BASES = [
    "https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/",
    "https://gh-proxy.com/https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/",
    "https://ghproxy.net/https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/",
]
MANIFEST_RELPATH = "package.v3.json"
BIN_RELPATH = "resources.v3/user.sites.v3.bin"

DEFAULT_KEY = b"c1qlByOVxi1-AnpLlYqJwP74XV9mF4GpKWUyjW1dXL8="
BACKUP_KEEP = 3

KEY_PROBE_CODE = r'''
import sys, types
from unittest.mock import MagicMock
import importlib.abc, importlib.machinery

class StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        m = types.ModuleType(spec.name); m.__path__ = []
        m.__dict__["__getattr__"] = lambda attr: MagicMock()
        return m
    def exec_module(self, module): pass

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "app" or name.startswith("app."):
            return importlib.machinery.ModuleSpec(name, StubLoader(), is_package=True)
        return None

sys.meta_path.insert(0, Finder())

import cryptography.fernet as _f
_RealF = _f.Fernet
CAPTURED = []

class Probe(_RealF):
    def __init__(self, key, *a, **k):
        try: CAPTURED.append(bytes(key))
        except Exception: pass
        super().__init__(key, *a, **k)

_f.Fernet = Probe
import sites

class FakeSettings:
    GUID = "G"; VERSION_FLAG = "v3"; RESOURCE_VERSION_FLAG = "v3"
    def __getattr__(self, item): return "SET:" + item
sites.settings = FakeSettings()

try:
    sites.SitesHelper()
except Exception:
    pass

print(CAPTURED[-1].decode() if CAPTURED else "")
'''


# ---------------------------------------------------------------- 基础读写

def decrypt_bin(raw: bytes, key: bytes) -> Dict[str, Any]:
    """bin → dict（自动识别 Fernet 外壳与裸 pickle）。"""
    if raw.startswith(b"gAAAAAB"):
        plain = Fernet(key).decrypt(raw)
    else:
        plain = raw
    return pickle.loads(plain)


def encrypt_bin(obj: Dict[str, Any], key: bytes) -> bytes:
    return Fernet(key).encrypt(pickle.dumps(obj, protocol=5))


def bump_version(version: str) -> str:
    parts = str(version).split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def vtuple(v: str):
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def overlay_fingerprint(sites: List[Dict[str, Any]]) -> str:
    """overlay 内容指纹（canonical JSON, sha256 前 12 位）。幂等判定的依据。"""
    canonical = json.dumps(sites, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def overlay_in_bin(obj: Dict[str, Any], sites: List[Dict[str, Any]]) -> bool:
    """overlay 是否已按当前内容在位（每个 id 存在且指纹一致）。"""
    idx = obj.get("indexers") or {}
    for site in sites:
        current = idx.get(site["id"])
        if not current:
            return False
        a = json.dumps(current, ensure_ascii=False, sort_keys=True, default=str)
        b = json.dumps(site, ensure_ascii=False, sort_keys=True, default=str)
        if hashlib.sha256(a.encode()).hexdigest() != hashlib.sha256(b.encode()).hexdigest():
            return False
    return True


def merge_overlay(obj: Dict[str, Any], sites: List[Dict[str, Any]]) -> List[str]:
    """把 overlay 合并进 bin dict，返回变更说明。"""
    idx = obj.setdefault("indexers", {})
    changes = []
    for site in sites:
        changes.append(("覆盖" if site["id"] in idx else "新增") + ":" + site["id"])
        idx[site["id"]] = site
    return changes


def official_version_from_manifest(manifest_bytes: bytes) -> str:
    manifest = json.loads(manifest_bytes)
    res = (manifest.get("resources") or {}).get("user.sites.v3.bin") or {}
    return str(res.get("version") or "")


# ---------------------------------------------------------------- 维护编排

class BinMaintainer:
    """bin 自维护编排器。fetch(url) -> (status, bytes) | None 由宿主注入。"""

    def __init__(self,
                 key: bytes,
                 overlay_sites: List[Dict[str, Any]] = None,
                 fetch: Callable[[str], Optional[Tuple[int, bytes]]] = None,
                 log: Any = None):
        self.key = key
        self.overlay = overlay_sites if overlay_sites is not None else OVERLAY_SITES
        self.fetch = fetch
        self.log = log or (lambda *a: None)
        self._lock = threading.Lock()

    # ---- 网络 ----

    def _get(self, relpath: str) -> Optional[bytes]:
        if self.fetch is None:
            return None
        for base in RAW_BASES:
            try:
                r = self.fetch(base + relpath)
                if r and r[0] == 200 and r[1]:
                    return r[1]
            except Exception as e:  # noqa: BLE001
                self.log(f"镜像不可用 {base.split('/')[2]}: {e}")
        return None

    # ---- key 轮换 ----

    def try_recapture_key(self, sites_module_file: Path, encrypted_bin: bytes) -> Optional[bytes]:
        """key 轮换兜底: 子进程探针（在 MP 容器内解释器必然匹配）。"""
        try:
            suffix = sites_module_file.suffix
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                shutil.copy2(sites_module_file, td / f"sites{suffix}")
                (td / "user.sites.v3.bin").write_bytes(encrypted_bin)
                r = subprocess.run([sys.executable, "-c", KEY_PROBE_CODE],
                                   cwd=td, capture_output=True, text=True, timeout=180)
                out = (r.stdout or "").strip().splitlines()
                key = out[-1].strip() if out else ""
                if key:
                    return key.encode()
        except Exception as e:  # noqa: BLE001
            self.log(f"key 探针异常: {e}")
        return None

    # ---- 主流程 ----

    def run(self, installed_bin_path: Path, key_source: Optional[Callable[[], None]] = None,
            sites_module_file: Optional[Path] = None) -> Dict[str, Any]:
        """执行一次维护。返回 {"action": skip|merged|error, ...}。任何失败不动原文件。"""
        with self._lock:
            return self._run_unlocked(installed_bin_path, key_source, sites_module_file)

    def _run_unlocked(self, installed_bin_path: Path, key_source, sites_module_file) -> Dict[str, Any]:
        result: Dict[str, Any] = {"action": "skip", "detail": ""}
        path = Path(installed_bin_path)
        if not path.exists():
            result.update(action="error", detail=f"bin 不存在: {path}")
            return result
        raw = path.read_bytes()

        # 1. 已装 bin 状态
        key = self.key
        try:
            installed = decrypt_bin(raw, key)
        except (InvalidToken, ValueError):
            if key_source and sites_module_file is not None:
                new_key = self.try_recapture_key(sites_module_file, raw)
                if new_key:
                    self.key = key = new_key
                    if key_source:
                        key_source()
                    try:
                        installed = decrypt_bin(raw, key)
                    except Exception as e:  # noqa: BLE001
                        result.update(action="error", detail=f"新 key 仍解不开已装 bin: {e}")
                        return result
                else:
                    result.update(action="error",
                                  detail="key 解不开已装 bin 且探针未取到新 key，需要人工确认")
                    return result
            else:
                result.update(action="error", detail=f"已装 bin 解密失败: key 不匹配")
                return result
        except Exception as e:  # noqa: BLE001
            result.update(action="error", detail=f"已装 bin 读取失败: {e}")
            return result

        overlay_ok = overlay_in_bin(installed, self.overlay)
        installed_version = str(installed.get("version") or "0")

        # 2. 官方版本（清单，小请求）
        manifest = self._get(MANIFEST_RELPATH)
        if manifest is None:
            if overlay_ok:
                result.update(detail="官方清单不可达，overlay 在位，跳过")
                return result
            result.update(action="error", detail="官方清单不可达且 overlay 不在位，无法合并")
            return result
        official_version = official_version_from_manifest(manifest)

        # 3. 判定是否需要合并
        #    期望状态: overlay 在位 且 版本 = 官方+1（我们的合并态）
        #    不满足的三种情况都走合并: overlay 缺失(MP 覆盖过) / 版本落后 / 官方刚 bump
        expected_merged_version = bump_version(official_version)
        if overlay_ok and installed_version == expected_merged_version:
            result.update(detail=f"overlay 在位且与官方 {official_version} 同步")
            return result

        # 4. 拉官方 bin
        official_raw = self._get(BIN_RELPATH)
        if official_raw is None:
            result.update(action="error", detail="官方 bin 下载失败")
            return result
        try:
            official_obj = decrypt_bin(official_raw, key)
        except (InvalidToken, ValueError):
            result.update(action="error",
                          detail="官方 bin 与当前 key 不匹配（疑似密钥轮换且探针未启用），跳过")
            return result

        # 5. 合并 + 版本 = 官方+1
        changes = merge_overlay(official_obj, self.overlay)
        official_obj["version"] = bump_version(str(official_obj.get("version") or official_version))
        fingerprint = overlay_fingerprint(self.overlay)

        # 6. 原子写回 + 备份
        self._atomic_write(path, official_obj, key)
        result.update(action="merged",
                      detail=f"version={official_obj['version']} 指纹={fingerprint} 变更={','.join(changes) or '无'}")
        return result

    def _atomic_write(self, path: Path, obj: Dict[str, Any], key: bytes) -> None:
        """备份(保留最近 N 份) + 临时文件 + os.replace 原子替换。"""
        stamp = time.strftime("%Y%m%d%H%M%S")
        backup = path.with_name(path.name + f".bak-{stamp}")
        shutil.copy2(path, backup)
        backups = sorted(path.parent.glob(path.name + ".bak-*"))
        for old in backups[:-BACKUP_KEEP] if len(backups) > BACKUP_KEEP else []:
            old.unlink(missing_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(encrypt_bin(obj, key))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
