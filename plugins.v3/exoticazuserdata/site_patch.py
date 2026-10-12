# -*- coding: utf-8 -*-
"""
Sukebei 域名白名单补丁核心(多锚点 + AST 等价校验 + 失败保真 + 幂等 + 回滚)

与 CLI 工具 apply_patch.py 共享同一套防线设计; 本模块为插件内嵌版本,
提供磁盘态探测(已应用/待生效/未应用)与进程启动快照(供 UI 区分"待重启")。
"""
import ast
import os
import re
import shutil
import tempfile
from pathlib import Path

TARGET_DOMAIN = "sukebei.nyaa.si"
# 相对 MP 根目录的目标文件(多版本稳定, v3 起路径未变)
RELATIVE_FILE = Path("app") / "domain" / "site.py"

# ---------------------------------------------------------------- 多锚点定义
# 每个锚点 = (名称, 正则)。全部命中才认为文件结构与我们认知一致。
# 锚点选取原则: 覆盖声明、消费、函数边界三个正交维度, 且各自足够独特。

ANCHORS = {
    # 锚点1: 常量声明存在且是元组起始
    "declaration": r"_SPECIAL_SITE_DOMAINS\s*=\s*\(",
    # 锚点2: 常量被 extract_domain 消费(证明消费关系未变)
    "consumption": r"for\s+domain\s+in\s+_SPECIAL_SITE_DOMAINS\s*:",
    # 锚点3: 消费发生在 extract_domain 函数体内(函数边界锚)
    "function_scope": r"def\s+extract_domain\s*\(\s*url\s*:\s*str\s*\)\s*->\s*str\s*:",
    # 锚点4: 已知先例条目存在(dmhy 系, 官方最早加入的条目, 多版本稳定)
    "known_member": r"[\"']u2\.dmhy\.org[\"']",
    # 锚点5: 元组以右括号收尾且紧随 urls_match 函数定义(锚定真实文件结构)
    "tuple_close": r"[\"'][^\"']+[\"']\s*,?\s*\)\s*\n\n\ndef\s+urls_match",
}


def fail(msg: str, code: int = 1) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(code)


def ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def check_anchors(src: str) -> None:
    """多锚点结构校验: 全部命中才放行; 缺失时抛 SystemExit(CLI 兼容)。"""
    import re
    missing = [name for name, pat in ANCHORS.items()
               if not re.search(pat, src)]
    if missing:
        raise SystemExit(f"结构锚点缺失: {missing} —— 文件结构与预期不符, 拒绝打补丁")


def domain_in_constant(src: str) -> bool:
    """AST 级判断: _SPECIAL_SITE_DOMAINS 元组中是否已含目标域名。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        fail(f"目标文件不是合法 Python: {e}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_SPECIAL_SITE_DOMAINS"
                for t in node.targets):
            if isinstance(node.value, ast.Tuple):
                return any(isinstance(e, ast.Constant) and e.value == TARGET_DOMAIN
                           for e in node.value.elts)
    return False


def ast_structure_equivalent(src_before: str, src_after: str) -> None:
    """AST 结构等价校验: 补丁只允许"常量元组多一个字符串元素"。

    对比两份源码的 AST 摘要(剥离常量元组的元素个数差异后必须完全一致),
    防止"小代码段一致但本质结构变了"的错补。
    """

    def summarize(src: str, drop_domain: bool) -> str:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_SPECIAL_SITE_DOMAINS"
                    for t in node.targets):
                if isinstance(node.value, ast.Tuple):
                    elts = [e for e in node.value.elts
                            if not (drop_domain and isinstance(e, ast.Constant)
                                    and e.value == TARGET_DOMAIN)]
                    node.value.elts = elts  # type: ignore[attr-defined]
        return ast.dump(tree)

    a, b = summarize(src_before, drop_domain=True), summarize(src_after, drop_domain=True)
    if a != b:
        fail("AST 结构等价校验失败: 补丁引入了目标之外的结构变化, 拒绝写入")


def build_patched(src: str) -> str:
    """生成补丁后源码: 在常量元组的第一个条目前插入目标域名(保持原缩进)。"""
    import re
    m = re.search(r"_SPECIAL_SITE_DOMAINS\s*=\s*\(\s*\n", src)
    if not m:
        fail("无法定位常量元组声明")
    lines = src.splitlines(keepends=True)
    decl_line = src[:m.end()].count("\n")          # 声明行行号(0 基)
    insert_idx = decl_line + 1                      # 首个条目行
    if insert_idx >= len(lines) or not lines[insert_idx].strip().startswith(('"', "'")):
        fail("常量元组的首行不是字符串条目, 结构异常")
    indent = lines[insert_idx][:len(lines[insert_idx]) - len(lines[insert_idx].lstrip())]
    lines.insert(insert_idx, f'{indent}"{TARGET_DOMAIN}",\n')
    return "".join(lines)



# 进程启动时的补丁状态快照(模块首次导入时记录, 之后不变——用于 UI 区分"已生效/待重启")
_BOOT_SNAPSHOT = None


def boot_snapshot(target_root) -> bool:
    """记录进程启动时补丁是否已在磁盘上(仅首次调用生效)。"""
    global _BOOT_SNAPSHOT
    if _BOOT_SNAPSHOT is None:
        try:
            _BOOT_SNAPSHOT = domain_in_constant((Path(target_root) / RELATIVE_FILE).read_text(encoding="utf-8"))
        except Exception:
            _BOOT_SNAPSHOT = False
    return _BOOT_SNAPSHOT


def _runtime_module_has_domain() -> Optional[bool]:
    """检查【运行中进程】的 app.domain.site 模块常量是否含目标域名。

    这是"已生效"的真实判据(模块加载视角); 模块未加载或无法读取返回 None。
    """
    try:
        import sys as _sys
        site_mod = _sys.modules.get("app.domain.site")
        if site_mod is None or not getattr(site_mod, "__file__", None):
            return None
        return domain_in_constant(open(site_mod.__file__, encoding="utf-8").read())
    except Exception:
        return None


def patch_state(target_root, enabled: bool, effective: Optional[bool] = None) -> str:
    """返回 UI 状态卡用的补丁状态码: applied / pending_restart / off / orphan / error。

    判据(三层, 2026-10-12 v2):
      applied          = 开关开 + 磁盘有 + 运行模块有 + 生命周期标记 effective=True
                         (三者齐才绿——缺任何一层都可能是残留/刚写入, 不得谎报)
      pending_restart  = 开关开 + 磁盘有 + (运行模块无 或 生命周期未确认生效)
      其余同旧语义; 开关关时探测失败归 off。
    effective=None 时退回运行模块判据(兼容 CLI 工具)。
    """
    file_path = Path(target_root) / RELATIVE_FILE
    try:
        on_disk = domain_in_constant(file_path.read_text(encoding="utf-8"))
    except Exception:
        return "off" if not enabled else "error"
    if not enabled and not on_disk:
        return "off"
    if enabled and on_disk:
        runtime = _runtime_module_has_domain()
        if runtime is None:
            return "pending_restart"   # 宿主外: 保守不谎报
        if runtime is True and effective is not False:
            # 运行模块有补丁; effective 明确 False 时仍视为待重启(刚写入)
            return "applied"
        return "pending_restart"
    if not enabled and on_disk:
        return "orphan"
    if enabled and not on_disk:
        return "error"
    return "off"


def apply_or_remove(target_root, remove: bool) -> Tuple[bool, str]:
    """应用/移除补丁。返回 (成功, 消息)。失败保真: 任何校验不过原文件不动。"""
    import sys as _sys
    file_path = Path(target_root) / RELATIVE_FILE
    if not file_path.exists():
        return False, f"目标文件不存在: {file_path}"
    src = file_path.read_text(encoding="utf-8")
    try:
        check_anchors(src)
    except SystemExit:
        return False, "结构锚点缺失, 文件结构与预期不符"
    already = domain_in_constant(src)
    # 终极防线(防无限追加): 正常结构中消费循环恰好 1 个;
    # 若 >1 说明历史上发生过重复追加, 立即拒绝写入并告警
    loop_count = len(re.findall(r"for\s+domain\s+in\s+_SPECIAL_SITE_DOMAINS\s*:", src))
    if loop_count > 1:
        return False, (f"检测到 {loop_count} 处消费循环(正常应为 1)——"
                       f"历史上已发生重复追加, 拒绝写入以防循环恶化, 请人工检查文件")
    if remove:
        if not already:
            return True, "目标域名不在常量中, 无需移除"
        patched = re.sub(r'\s*"' + TARGET_DOMAIN + r'",', "", src, count=1)
    else:
        if already:
            return True, "目标域名已在常量中, 幂等无操作"
        patched = build_patched(src)
        # 写入前二次校验: 补丁后循环数必须仍为 1
        patched_loops = len(re.findall(
            r"for\s+domain\s+in\s+_SPECIAL_SITE_DOMAINS\s*:", patched))
        if patched_loops != 1:
            return False, f"补丁后消费循环数为 {patched_loops}(应为 1), 拒绝写入"
    try:
        ast_structure_equivalent(src, patched)
        ast.parse(patched)
    except SystemExit:
        return False, "AST 结构等价/语法校验失败"
    except SyntaxError as e:
        return False, f"补丁后语法错误: {e}"
    backup = file_path.with_suffix(".py.bak-applypatch")
    shutil.copy2(file_path, backup)
    fd, tmp = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(patched)
        os.replace(tmp, file_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True, ("已追加" if not remove else "已移除") + f" {TARGET_DOMAIN} (备份: {backup.name})"
