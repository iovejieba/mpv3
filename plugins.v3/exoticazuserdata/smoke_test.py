#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
容器内冒烟测试: 一次抓取 patch 功能的全部事实, 定位状态卡与现实的偏差。

在 MP 容器内执行:
    docker exec moviepilot-v3 python /app/app/plugins/exoticazuserdata/smoke_test.py
"""
import json
import sys


def main() -> int:
    print("=" * 60)
    print("[1] 插件实例配置(sukebei_patch 开关的真实存储值)")
    try:
        from app.sdk.config import settings  # noqa: F401
    except ImportError:
        pass
    try:
        from app.db.oper.plugininstance import PluginInstanceOper
        rows = PluginInstanceOper().list() if hasattr(PluginInstanceOper, "list") else []
        for r in rows or []:
            name = getattr(r, "id", "") or ""
            if "exoticaz" in str(name).lower():
                cfg = getattr(r, "config_data", None)
                print(f"    实例 {name}: config = {cfg}")
    except Exception as e:
        print(f"    (DB 直查不可用: {e})")

    print("[2] 插件运行实例的内存开关值")
    try:
        from app.runtime.extensions.plugin.manager import get_plugin_manager
        pm = get_plugin_manager()
        inst = pm.get_running("ExoticaZUserData") if hasattr(pm, "get_running") else None
        if inst is None and hasattr(pm, "get_module"):
            cls = pm.get_module("ExoticaZUserData")
            inst = cls
        if inst is not None and hasattr(inst, "_sukebei_patch"):
            print(f"    _sukebei_patch = {inst._sukebei_patch}")
            print(f"    _enable        = {getattr(inst, '_enable', '?')}")
        else:
            print("    (运行实例未找到或无该属性)")
    except Exception as e:
        print(f"    (异常: {e})")

    print("[3] 磁盘 site.py 补丁状态")
    try:
        from app.domain import site as site_mod
        content = open(site_mod.__file__, encoding="utf-8").read()
        print(f"    文件: {site_mod.__file__}")
        print(f"    含 sukebei: {'sukebei' in content}")
    except Exception as e:
        print(f"    (异常: {e})")

    print("[4] 运行模块内存视角")
    try:
        import app.domain.site as site_mod
        src = open(site_mod.__file__, encoding="utf-8").read()
        print(f"    sys.modules 模块文件含 sukebei: {'sukebei' in src}")
    except Exception as e:
        print(f"    (异常: {e})")

    print("[5] 插件缓存(seeding_cache 里的生命周期标记)")
    try:
        from app.sdk.plugin.base import _PluginBase
        data = _PluginBase.get_data(None, "seeding_cache") if False else None
    except Exception:
        pass
    try:
        import sqlite3, glob
        # 插件数据通常在 PLUGIN_DATA_PATH/ExoticaZUserData/
        from app.runtime.settings import get_runtime_setting
        p = get_runtime_setting("PLUGIN_DATA_PATH")
        print(f"    PLUGIN_DATA_PATH = {p}")
    except Exception as e:
        print(f"    (异常: {e})")

    print("[6] 状态卡会算出的 state(用真实 _sukebei_patch 值)")
    try:
        sys.path.insert(0, "/app/app/plugins/exoticazuserdata")
        from exoticazuserdata import site_patch as sp
        # 用第 2 步抓到的内存值(或默认)模拟状态卡判定
        enabled = None
        try:
            from app.runtime.extensions.plugin.manager import get_plugin_manager
            pm = get_plugin_manager()
            inst = pm.get_running("ExoticaZUserData") if hasattr(pm, "get_running") else None
            if inst is not None:
                enabled = bool(inst._sukebei_patch)
        except Exception:
            pass
        if enabled is None:
            print("    (无法确定运行实例开关值, 跳过)")
        else:
            from app.domain import site as site_mod
            root = str(site_mod.__file__).rsplit(os.sep, 2)[0] if (os := __import__("os")) else None
            state = sp.patch_state(root, enabled=enabled,
                                   effective=None)
            print(f"    _sukebei_patch={enabled} → state = {state}")
    except Exception as e:
        print(f"    (异常: {e})")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
