"""
concurrenttools 改名兼容垫片测试模块

覆盖 python-concurrenttools 0.1.8（旧名健在）、0.1.9（改名后）、
半改名（只有一代里的一部分符号）与假想的未来大改版（两代名字都消失）
四种环境形态，以及幂等性、缺库让位与返回值契约。
"""

import sys
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

from utils.concurrenttools_compat import (
    _PATCHED_FLAG,
    patch_concurrenttools,
)


def _make_concurrenttools(**symbols: object) -> ModuleType:
    """构造一个带指定符号的假 concurrenttools 模块并挂到 sys.modules。"""
    module = ModuleType("concurrenttools")
    for name, value in symbols.items():
        setattr(module, name, value)
    sys.modules["concurrenttools"] = module
    return module


def _import_old_names() -> tuple[object, object]:
    """以 p115client 完全一致的 ``from concurrenttools import`` 形式取旧名。"""
    from concurrenttools import taskgroup_map, threadpool_map  # noqa: F401

    return threadpool_map, taskgroup_map


class ConcurrenttoolsCompatTest(TestCase):
    """垫片在不同 concurrenttools 形态下的行为"""

    def setUp(self) -> None:
        self._saved = sys.modules.get("concurrenttools")
        sys.modules.pop("concurrenttools", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            sys.modules["concurrenttools"] = self._saved
        else:
            sys.modules.pop("concurrenttools", None)

    def test_018_old_names_untouched(self) -> None:
        """0.1.8 旧名健在时为零操作，不新增任何别名属性，返回空列表"""

        def threadpool_map(func, it, /, *its, max_workers=None):
            return map(func, it, *its)

        async def taskgroup_map(func, it, /, *its, max_workers=None):
            yield map(func, it, *its)

        module = _make_concurrenttools(
            threadpool_map=threadpool_map,
            taskgroup_map=taskgroup_map,
        )

        applied = patch_concurrenttools()

        self.assertEqual(applied, [], "旧名健在时不应报告任何挂载")
        self.assertIs(module.threadpool_map, threadpool_map)
        self.assertIs(module.taskgroup_map, taskgroup_map)
        self.assertNotIn("thread_conmap", vars(module))
        self.assertNotIn("async_conmap", vars(module))
        self.assertTrue(getattr(module, _PATCHED_FLAG))

    def test_019_renamed_names_backfilled(self) -> None:
        """0.1.9 只有新名时，旧名回填为同一函数对象且 from-import 可用"""

        def thread_conmap(func, it, /, *its, max_workers=None):
            return map(func, it, *its)

        async def async_conmap(func, it, /, *its, max_workers=None):
            yield map(func, it, *its)

        module = _make_concurrenttools(
            thread_conmap=thread_conmap,
            async_conmap=async_conmap,
        )

        applied = patch_concurrenttools()

        self.assertEqual(
            applied, ["threadpool_map", "taskgroup_map"], "应报告两条挂载"
        )
        backfilled_threadpool, backfilled_taskgroup = _import_old_names()
        self.assertIs(backfilled_threadpool, thread_conmap)
        self.assertIs(backfilled_taskgroup, async_conmap)

    def test_partial_mapping_only_applies_available_side(self) -> None:
        """半改名环境：只挂有新名对应的那条，另一条不造符号"""

        async def async_conmap(func, it, /, *its, max_workers=None):
            yield map(func, it, *its)

        module = _make_concurrenttools(async_conmap=async_conmap)

        applied = patch_concurrenttools()

        self.assertEqual(applied, ["taskgroup_map"], "只应报告 taskgroup_map")
        self.assertFalse(
            hasattr(module, "threadpool_map"), "无新名对应时不应造符号"
        )
        self.assertIs(module.taskgroup_map, async_conmap)

    def test_idempotent(self) -> None:
        """重复调用不叠加、不改变已回填的对象，第二次返回空列表"""

        def thread_conmap(func, it, /, *its, max_workers=None):
            return map(func, it, *its)

        async def async_conmap(func, it, /, *its, max_workers=None):
            yield map(func, it, *its)

        module = _make_concurrenttools(
            thread_conmap=thread_conmap,
            async_conmap=async_conmap,
        )

        first = patch_concurrenttools()
        first_threadpool = module.threadpool_map
        second = patch_concurrenttools()

        self.assertEqual(first, ["threadpool_map", "taskgroup_map"])
        self.assertEqual(second, [], "重复调用不应再次挂载")
        self.assertIs(module.threadpool_map, first_threadpool)
        self.assertIs(module.taskgroup_map, async_conmap)

    def test_both_generations_missing_is_silent(self) -> None:
        """两代名字都不存在时不抛异常、不造符号，保留 p115client 原生报错"""
        module = _make_concurrenttools(conmap=lambda f, it: it)

        applied = patch_concurrenttools()

        self.assertEqual(applied, [])
        self.assertFalse(hasattr(module, "threadpool_map"))
        self.assertFalse(hasattr(module, "taskgroup_map"))
        self.assertFalse(hasattr(module, "thread_conmap"))

    def test_missing_module_yields_to_p115client(self) -> None:
        """concurrenttools 整个缺失时静默让位，不抛异常不造报错点"""

        with patch.dict(sys.modules, {"concurrenttools": None}):
            applied = patch_concurrenttools()

        self.assertEqual(
            applied, [], "缺库时应返回空列表，让 p115client 抛原生错误"
        )

    def test_real_concurrenttools_invariant(self) -> None:
        """真实环境不变式：只要任一代 map 存在，patch 后旧名必然可用"""
        applied = patch_concurrenttools()

        self.assertIsInstance(applied, list)
        module = sys.modules["concurrenttools"]
        has_old = hasattr(module, "threadpool_map") or hasattr(
            module, "taskgroup_map"
        )
        has_new = hasattr(module, "thread_conmap") or hasattr(
            module, "async_conmap"
        )
        if has_old or has_new:
            threadpool_map, taskgroup_map = _import_old_names()
            self.assertIsNotNone(threadpool_map)
            self.assertIsNotNone(taskgroup_map)
