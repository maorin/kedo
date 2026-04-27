"""
ProfileGuard — 写文件拦截器

四层防御（按优先级从高到低）：
  1. ProjectProfile 层：.kedo/project_profile.json 是 human_verified 时拒写
  2. **Charter 层**（方案 C）：.kedo/project_charter.md 存在时按 Charter.violations 拦
     - charter.build.forbidden_files 命中
     - charter.artifact.target_name rename 不同步
  3. 双 build system 并存（charter 缺失时的硬编码降级）
  4. 结构完整性层：Makefile/CMakeLists 重写时 critical target/call 不能消失

LLM 拿到拒写错误后会知道得换增量编辑（file_edit）或调 propose_charter_change，
不要整体覆盖 / 引入对家 build system。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from core.project_charter import Charter

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

        # ---- 第二层：Charter 驱动（方案 C）----
        # charter 存在时优先用 charter.violations；charter 缺失时降级到第三层硬编码
        charter = self._load_charter()
        charter_has_forbidden = bool(charter and charter.forbidden_files)
        charter_has_target = bool(charter and charter.target_name)

        if charter is not None:
            try:
                vio = charter.violations(str(p), new_content, project_path=self._project_path)
            except Exception as e:
                logger.warning(f"Charter.violations raised: {e}")
                vio = []
            if vio:
                # 只返回第一条最关键的违约（多条情况罕见）
                return vio[0]

        # ---- 第三层：双 build system 并存（charter 没接管 forbidden_files 时的降级）----
        if not charter_has_forbidden:
            coexist_block = self._check_no_coexist(p)
            if coexist_block:
                return coexist_block

        # ---- 第四层：构建文件结构完整性 ----
        name_lower = p.name.lower()
        if name_lower in ("makefile",) or p.name in ("Makefile", "makefile"):
            return self._check_makefile(p, new_content)
        if p.name == "CMakeLists.txt":
            structural = self._check_cmakelists(p, new_content)
            if structural:
                return structural
            # charter 已经接管 target_name 检查时跳过这条降级
            if not charter_has_target:
                return self._check_cmake_target_rename(p, new_content)

        return None

    def _load_charter(self) -> Optional[Charter]:
        """每次 check 重新 load charter，propose_charter_change 写盘后立刻生效。"""
        if not self._project_path:
            return None
        try:
            return Charter.load(self._project_path)
        except Exception as e:
            logger.debug(f"ProfileGuard: charter load failed: {e}")
            return None

    def _check_no_coexist(self, p: Path) -> Optional[str]:
        """新建 Makefile 时若项目根已有 CMakeLists.txt 则拒，反之亦然。
        只对项目根生效（子目录的 Makefile 是 CMake 自己生成的，不拦）。
        已存在文件的覆盖不拦（_check_makefile / _check_cmakelists 自有保护）。"""
        if not self._project_path:
            return None
        try:
            project_root = Path(self._project_path).resolve()
        except (OSError, RuntimeError):
            return None
        # 仅项目根目录的写入需要拦截
        if p.parent != project_root:
            return None
        # 已存在的文件交给后续 _check_* 做结构保护，这里只拦"新建对家"
        if p.exists():
            return None

        is_new_makefile = p.name in ("Makefile", "makefile", "GNUmakefile")
        is_new_cmake = p.name == "CMakeLists.txt"
        if not (is_new_makefile or is_new_cmake):
            return None

        cmake_existing = (project_root / "CMakeLists.txt").exists()
        makefile_existing = any(
            (project_root / n).exists() for n in ("Makefile", "makefile", "GNUmakefile")
        )

        if is_new_makefile and cmake_existing:
            return (
                f"Refusing to create {p.name}: project already has CMakeLists.txt. "
                f"Two build systems in the same project conflict (CMake also generates "
                f"its own Makefile under build/). Either modify CMakeLists.txt to fix "
                f"the build, or delete CMakeLists.txt first if you really want to switch "
                f"to a hand-written Makefile (and update profile.build.command + "
                f"profile.deploy.command accordingly)."
            )
        if is_new_cmake and makefile_existing:
            return (
                f"Refusing to create CMakeLists.txt: project already has a Makefile. "
                f"Two build systems in the same project conflict. Either modify the "
                f"existing Makefile, or delete it first if you really want to switch "
                f"to CMake (and update profile.build.command accordingly)."
            )
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

    _CMAKE_TARGET_RE = re.compile(
        r"add_(?:executable|library)\s*\(\s*([A-Za-z_][\w-]*)",
        re.IGNORECASE,
    )

    def _check_cmake_target_rename(self, p: Path, new_content: str) -> Optional[str]:
        """CMakeLists 里 add_executable/library 的 target 名 rename 时，
        若 profile.deploy.command 还在引用旧 target，就拒写。
        switchvideo 这次：CMakeLists target 从 NfsVideoPlayer 改成 switchvideo，
        但 profile.deploy.command 还在 nxlink build/NfsVideoPlayer.nro，部署找不到产物。"""
        if not p.exists() or not self._mgr or not self._project_path:
            return None
        try:
            old = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        old_targets = set(self._CMAKE_TARGET_RE.findall(old))
        new_targets = set(self._CMAKE_TARGET_RE.findall(new_content))
        # 没旧 target / 没新 target / 完全相同 → 无 rename
        if not old_targets or not new_targets or old_targets == new_targets:
            return None
        removed = old_targets - new_targets
        if not removed:
            return None
        try:
            profile = self._mgr.load(self._project_path)
        except Exception as e:
            logger.debug(f"ProfileGuard: profile load failed for target rename check: {e}")
            return None
        if not profile:
            return None
        deploy_cmd = (profile.get("deploy", {}) or {}).get("command", "") or ""
        if not deploy_cmd:
            return None
        # 老 target name 整词出现在 deploy.command 里 → 部署会断
        for t in removed:
            if re.search(rf"\b{re.escape(t)}\b", deploy_cmd):
                return (
                    f"Refusing to rewrite CMakeLists.txt: would rename CMake target "
                    f"{sorted(removed)} → {sorted(new_targets)}, but profile.deploy.command "
                    f"still references the old name '{t}': {deploy_cmd!r}. "
                    f"After this write, deploy will look for a binary that no longer exists. "
                    f"Either keep the old target name, or update .kedo/project_profile.json "
                    f"deploy.command in the same change."
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
