"""
ProfileGuard — 写文件拦截器

两层防御：
  1. ProjectProfile 层：.kedo/project_profile.json 是 human_verified 时拒写
  2. 结构完整性层：Makefile/CMakeLists 重写时 critical target / call 不能消失

LLM 拿到拒写错误后会知道得换增量编辑（file_edit）而不是整体覆盖。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ProfileGuard:
    def __init__(self, profile_manager=None, project_path: Optional[str] = None):
        self._mgr = profile_manager
        self._project_path = project_path

    def set_project_path(self, project_path: str):
        """REPL 切项目时由调用方调用"""
        self._project_path = project_path

    def check(self, file_path: str, new_content: str) -> Optional[str]:
        """返回拒写原因；None = 放行"""
        try:
            p = Path(file_path).resolve()
        except (OSError, RuntimeError):
            return None

        rel_str = self._relative_to_project(p)

        # ---- 第一层：profile.json human_verified 保护 ----
        if rel_str == ".kedo/project_profile.json" and self._mgr and self._project_path:
            try:
                profile = self._mgr.load(self._project_path)
                if profile and profile.human_verified:
                    return (
                        ".kedo/project_profile.json 已被标记 human_verified，"
                        "auto-fix 无权覆盖。请改源代码 / 构建文件，或先用户手动取消标记。"
                    )
            except Exception as e:
                logger.debug(f"ProfileGuard: profile load failed: {e}")

        # ---- 第二层：构建文件结构完整性 ----
        name_lower = p.name.lower()
        if name_lower in ("makefile",) or p.name in ("Makefile", "makefile"):
            return self._check_makefile(p, new_content)
        if p.name == "CMakeLists.txt":
            return self._check_cmakelists(p, new_content)

        return None

    def _relative_to_project(self, p: Path) -> str:
        """计算相对项目根的路径；不可计算则返回绝对路径字符串"""
        if not self._project_path:
            return str(p)
        try:
            return str(p.relative_to(Path(self._project_path).resolve())).replace("\\", "/")
        except ValueError:
            return str(p)

    @staticmethod
    def _check_makefile(p: Path, new_content: str) -> Optional[str]:
        """Makefile: 旧的 critical target (all/build/clean/install) 不能在新内容里消失"""
        if not p.exists():
            return None
        try:
            old = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        # 行首 token: 后接空格或冒号，认作 target 定义
        target_re = re.compile(r"^([A-Za-z_.][\w./-]*)\s*:", re.MULTILINE)
        old_targets = set(target_re.findall(old))
        new_targets = set(target_re.findall(new_content))
        critical = {"all", "build", "clean", "install"} & old_targets
        missing = critical - new_targets
        if missing:
            return (
                f"Refusing to rewrite {p.name}: critical target(s) {sorted(missing)} "
                f"present in current file but missing from new content. "
                f"Make a targeted edit to LIBS/CFLAGS/SOURCES instead of full rewrite — "
                f"otherwise `make` will fail with 'no targets'."
            )
        return None

    @staticmethod
    def _check_cmakelists(p: Path, new_content: str) -> Optional[str]:
        """CMakeLists.txt: cmake_minimum_required / project() / add_executable 不能丢"""
        if not p.exists():
            return None
        try:
            old = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        checks = [
            ("cmake_minimum_required",  r"cmake_minimum_required\s*\("),
            ("project()",               r"\bproject\s*\("),
            ("add_executable/library",  r"add_(?:executable|library)\s*\("),
        ]
        missing = []
        for label, pat in checks:
            old_has = re.search(pat, old, re.IGNORECASE) is not None
            new_has = re.search(pat, new_content, re.IGNORECASE) is not None
            if old_has and not new_has:
                missing.append(label)
        if missing:
            return (
                f"Refusing to rewrite CMakeLists.txt: critical call(s) {missing} "
                f"present in current file but missing from new content. "
                f"Make a targeted edit instead of full rewrite — "
                f"otherwise `cmake` will refuse to configure."
            )
        return None
