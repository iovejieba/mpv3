"""
python-concurrenttools 0.1.9 改名兼容垫片。

背景：p115client 0.0.9.6.5.1 的 ``tool/upload.py`` 硬编码旧名::

    from concurrenttools import threadpool_map, taskgroup_map

而上游 python-concurrenttools 0.1.9（2026-09-29）把它们分别改名为
``thread_conmap`` / ``async_conmap``。两者签名与实现逐字等价，属纯改名；
p115client 用到的其余符号（conmap、run_as_thread、iter_page、iter_page_multi、
iter_offset）在 0.1.9 中均未变化。

requirements.txt 与 pyproject.toml 已显式锁定 ``python-concurrenttools<0.1.9``，
但 MoviePilot V3 宿主只在「插件已登记且判定依赖缺失」时才重新解析安装依赖：
手动拷贝部署、依赖重装失败或与其他插件的约束冲突，都会让 venv 里残留 0.1.9+，
此时 p115client 导入即崩（cannot import name 'threadpool_map'），插件加载失败。

本模块是锁版本失效时的运行时自愈手段：在任何 p115client 导入之前把 0.1.9 的
新名以旧名回填到 concurrenttools 模块对象上。``from X import Y`` 在导入时才对
模块对象做属性查找，因此先回填即可让 p115client 的旧名导入照常工作。

行为约定：
- 旧名已存在（0.1.8 等旧版本）→ 不做任何改动；
- 旧名缺失且新名存在 → 挂别名，旧名计入返回值；
- 新旧名都缺失（假想的未来大改版）→ 不造符号，让 p115client 抛出原生
  ImportError，不掩盖真实的不兼容；
- concurrenttools 本身不可导入 → 静默让位，由 p115client 抛出自然的
  ModuleNotFoundError，报错归因不偏移到本垫片；
- 进程内幂等（标志位短路），可安全重复调用。

:return 约定: 每次调用返回**本次新挂载**的旧名列表（无需挂载、已挂载过、
              挂载失败或缺库让位时为空列表），供调用方在启动日志中提示用户。
"""

import sys
from importlib import import_module

__all__ = ["patch_concurrenttools"]

# 已处理标记：concurrenttools 的 API 在进程内不会变化，处理一次即可
_PATCHED_FLAG = "_p115strmhelper_compat_patched"

# p115client 0.0.9.6.5.1 需要的旧名 -> concurrenttools 0.1.9 的新名候选
# （候选留成元组，将来若再出现多代改名可直接追加）
_ALIASES: dict[str, tuple[str, ...]] = {
    "threadpool_map": ("thread_conmap",),
    "taskgroup_map": ("async_conmap",),
}


def patch_concurrenttools() -> list[str]:
    """
    确保 concurrenttools 暴露 p115client 依赖的旧名。

    对 0.1.8（旧名健在）是零操作；对 0.1.9+ 把新名以旧名回填为同一函数对象。

    :return list[str]: 本次调用实际新挂载的旧名列表；无需挂载、已挂载过、
                       挂载失败或缺库让位时为空列表
    """
    module = sys.modules.get("concurrenttools")
    if module is None:
        try:
            module = import_module("concurrenttools")
        except Exception:
            # concurrenttools 整个缺失：不再制造第二个报错点，让 p115client
            # 自己抛出自然的 ModuleNotFoundError，归因指向真正的缺失依赖。
            return []

    if getattr(module, _PATCHED_FLAG, False):
        return []

    applied: list[str] = []
    for old_name, candidates in _ALIASES.items():
        if hasattr(module, old_name):
            continue
        for candidate in candidates:
            replacement = getattr(module, candidate, None)
            if replacement is None:
                continue
            try:
                setattr(module, old_name, replacement)
            except Exception:
                # 个别符号挂不上就放弃该条，不让垫片自己变成新的故障点
                break
            applied.append(old_name)
            break

    setattr(module, _PATCHED_FLAG, True)
    return applied
