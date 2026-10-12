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
    inst = None
    try:
        # 路径1: 模块级单例(MP 常见模式)
        import app.plugins.exoticazuserdata as plugin_mod
        for name in dir(plugin_mod):
            obj = getattr(plugin_mod, name)
            if isinstance(obj, type) and hasattr(obj, "_sukebei_patch"):
                pass
        # 路径2: 从 PluginManager 的运行表拿
        from app.runtime.extensions.plugin import manager as pm_mod
        pm_obj = None
        for name in dir(pm_mod):
            if name == "get_plugin_manager":
                pm_obj = getattr(pm_mod, name)()
                break
        if pm_obj is None:
            # 尝试 app.plugins 命名空间的已知引用
            for mod_name in ("app.plugins.exoticazuserdata",):
                mod = sys.modules.get(mod_name)
                if mod and hasattr(mod, "Plugin"):
                    pass
        # 直接找: lifecycle _running
        if pm_obj is not None and hasattr(pm_obj, "_lifecycle"):
            lc = pm_obj._lifecycle
            running = getattr(lc, "_running", {})
            inst = running.get("ExoticaZUserData")
        elif pm_obj is not None and hasattr(pm_obj, "get_running_plugin"):
            inst = pm_obj.get_running_plugin("ExoticaZUserData")
    except Exception as e:
        print(f"    (抓取异常: {e})")
    if inst is not None:
        print(f"    实例类型: {type(inst).__name__}")
        print(f"    _sukebei_patch = {getattr(inst, '_sukebei_patch', '?')}")
        print(f"    _enable        = {getattr(inst, '_enable', '?')}")
    else:
        print("    运行实例未找到")

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

    print("[2b] gc 扫描运行实例(最可靠)")
    try:
        import gc
        found = []
        for o in gc.get_objects():
            if type(o).__name__ == "ExoticaZUserData":
                found.append(o)
        if found:
            for o in found:
                print(f"    实例 {id(o)}: _sukebei_patch={o._sukebei_patch}, _enable={o._enable}")
        else:
            print("    gc 中无 ExoticaZUserData 实例")
        globals()["_found_inst"] = found[0] if found else None
    except Exception as e:
        print(f"    (异常: {e})")
        globals()["_found_inst"] = None

    print("[6] 状态卡会算出的 state(用真实运行实例的开关值)")
    try:
        sys.path.insert(0, "/app/app/plugins/exoticazuserdata")
        from exoticazuserdata import site_patch as sp
        enabled = None
        try:
            enabled = bool(globals().get("_found_inst")._sukebei_patch)
        except Exception:
            pass
        if enabled is None:
            print("    (无运行实例, 跳过)")
        else:
            from app.domain import site as site_mod
            root = str(site_mod.__file__).rsplit(os.sep, 2)[0]
            state = sp.patch_state(root, enabled=enabled, effective=None)
            print(f"    状态卡按 _sukebei_patch={enabled} → state = {state}")
    except Exception as e:
        print(f"    (异常: {e})")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
