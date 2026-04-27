"""
Project Charter — 项目契约（方案 C 共享层）

一份 Markdown + YAML frontmatter 文件 (.kedo/project_charter.md)，作为 Producer
和 Reviewer 共享的"项目宪法"：build_system、forbidden_files、target_name、
deploy.command 等不可漂移的契约都在这里。

设计动机（switchvideo 6075f8ec 暴露的根因）：项目"应该长什么样"的初始约定从未被
显式持久化，于是 Producer 在 50+ turn 后注意力分散时漂移到训练数据先验（"Switch
homebrew 标准是 devkitPro switch_rules Makefile"），新写 Makefile 的同时不删
CMakeLists.txt，导致双 build system 并存。Charter 把这种隐式约定显式化：
  1. 启动时 system prompt 注入（Producer + Reviewer 同读一份契约）
  2. ProfileGuard 写文件前检查 charter violation
  3. mutable=false 时 Producer 想改 charter 必须走 propose_charter_change 阻塞等人

文件格式：
  ---
  schema_version: 1
  mutable: false
  build:
    system: cmake
    forbidden_files: [Makefile, GNUmakefile, ...]
    must_have_files: [CMakeLists.txt]
    command: "cmake -B build ..."
  artifact:
    target_name: switchvideo
    output_path: build/switchvideo.nro
  ...
  ---
  # Project Charter — switchvideo
  (自由文本 body，给人和 LLM 看的 narrative)

charter 缺失（无 .kedo/project_charter.md）时 load() 返回 None，所有上层降级到
charter-less 旧行为（ProfileGuard 走硬编码、system prompt 不注入）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


CHARTER_REL_PATH = ".kedo/project_charter.md"

# YAML frontmatter 边界
_FRONTMATTER_RE = re.compile(
    r"^---\s*\r?\n(?P<yaml>.*?)\r?\n---\s*\r?\n?(?P<body>.*)$",
    re.DOTALL,
)

# 提取 CMakeLists 里 add_executable / add_library 的 target 名（与 ProfileGuard 同正则）
_CMAKE_TARGET_RE = re.compile(
    r"add_(?:executable|library)\s*\(\s*([A-Za-z_][\w-]*)",
    re.IGNORECASE,
)


@dataclass
class Charter:
    """加载后的 Charter，所有字段都有默认值（YAML 缺字段时用空值降级而非崩溃）。"""

    schema_version: int = 1
    mutable: bool = True
    last_changed: str = ""
    last_change_reason: str = ""
    project_kind: str = ""

    build: dict = field(default_factory=dict)
    artifact: dict = field(default_factory=dict)
    deploy: dict = field(default_factory=dict)
    coding_conventions: list = field(default_factory=list)
    forbidden_actions: list = field(default_factory=list)

    body_text: str = ""
    file_path: str = ""           # charter 文件绝对路径
    raw_yaml: dict = field(default_factory=dict)  # 原始 frontmatter（写回时复用未识别字段）

    # ----------------------------------------------------------
    # 加载 / 持久化
    # ----------------------------------------------------------

    @classmethod
    def path_for(cls, project_path: str) -> Path:
        return Path(project_path) / CHARTER_REL_PATH

    @classmethod
    def exists(cls, project_path: str) -> bool:
        try:
            return cls.path_for(project_path).exists()
        except Exception:
            return False

    @classmethod
    def load(cls, project_path: str) -> Optional["Charter"]:
        """读 .kedo/project_charter.md。文件不存在 → None；解析失败 → None + log warn。"""
        p = cls.path_for(project_path)
        if not p.exists():
            return None
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Charter read failed at {p}: {e}")
            return None

        m = _FRONTMATTER_RE.match(text)
        if not m:
            logger.warning(f"Charter at {p} has no YAML frontmatter; ignoring.")
            return None

        try:
            import yaml
        except ImportError:
            logger.warning("Charter: pyyaml not installed; charter cannot be loaded.")
            return None

        try:
            data = yaml.safe_load(m.group("yaml")) or {}
            if not isinstance(data, dict):
                logger.warning(f"Charter at {p}: frontmatter is not a mapping.")
                return None
        except Exception as e:
            logger.warning(f"Charter YAML parse failed at {p}: {e}")
            return None

        c = cls(
            schema_version=int(data.get("schema_version", 1)),
            mutable=bool(data.get("mutable", True)),
            last_changed=str(data.get("last_changed", "") or ""),
            last_change_reason=str(data.get("last_change_reason", "") or ""),
            project_kind=str(data.get("project_kind", "") or ""),
            build=dict(data.get("build") or {}),
            artifact=dict(data.get("artifact") or {}),
            deploy=dict(data.get("deploy") or {}),
            coding_conventions=list(data.get("coding_conventions") or []),
            forbidden_actions=list(data.get("forbidden_actions") or []),
            body_text=m.group("body") or "",
            file_path=str(p),
            raw_yaml=data,
        )
        return c

    def save(self, new_reason: str = "") -> None:
        """把当前实例写回 charter 文件，bump last_changed。
        propose_charter_change 工具 approve 后调用。"""
        try:
            import yaml
        except ImportError:
            raise RuntimeError("Charter.save requires pyyaml (pip install pyyaml)")
        # 把可能更新过的字段同步回 raw_yaml（保留未识别字段不丢）
        merged = dict(self.raw_yaml)
        merged.update({
            "schema_version": self.schema_version,
            "mutable": self.mutable,
            "last_changed": date.today().isoformat(),
            "last_change_reason": new_reason or self.last_change_reason,
            "project_kind": self.project_kind,
            "build": self.build,
            "artifact": self.artifact,
            "deploy": self.deploy,
            "coding_conventions": self.coding_conventions,
            "forbidden_actions": self.forbidden_actions,
        })
        # 把 None 值剔掉，避免 YAML 输出 null
        merged = {k: v for k, v in merged.items() if v not in (None, "", [], {})}

        yaml_text = yaml.safe_dump(
            merged, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        body = self.body_text or "# Project Charter\n\n(待补充)\n"
        body_normalized = body if body.startswith("\n") else "\n" + body.lstrip("\n")
        full = "---\n" + yaml_text + "---" + body_normalized
        Path(self.file_path).write_text(full, encoding="utf-8")
        # 同步更新内存里的 last_changed
        self.last_changed = merged["last_changed"]
        self.last_change_reason = merged["last_change_reason"]

    # ----------------------------------------------------------
    # 属性
    # ----------------------------------------------------------

    @property
    def frozen(self) -> bool:
        return not self.mutable

    @property
    def build_system(self) -> str:
        return str(self.build.get("system", "") or "").lower()

    @property
    def forbidden_files(self) -> list[str]:
        return [str(x) for x in (self.build.get("forbidden_files") or [])]

    @property
    def must_have_files(self) -> list[str]:
        return [str(x) for x in (self.build.get("must_have_files") or [])]

    @property
    def target_name(self) -> str:
        return str(self.artifact.get("target_name", "") or "")

    @property
    def build_command(self) -> str:
        return str(self.build.get("command", "") or "")

    @property
    def deploy_command(self) -> str:
        return str(self.deploy.get("command", "") or "")

    # ----------------------------------------------------------
    # 静态规则引擎
    # ----------------------------------------------------------

    def violations(
        self,
        file_path: str,
        new_content: str,
        project_path: Optional[str] = None,
    ) -> list[str]:
        """检查一次 file_write 是否违约。返回违约描述列表（空列表 = 放行）。

        所检查的规则（按代价从轻到重）：
          R1: 写项目根的 forbidden_files 之一 → 违约（双 build system 主拦点）
          R2: CMakeLists.txt 里 add_executable target rename，但 charter.artifact.target_name
              没同步 → 违约
          R3: .kedo/project_profile.json 的 build.command / deploy.command 与 charter
              对应字段不一致 → 违约（这条由 ProfileGuard 调用 .check_profile_consistency 走，
              这里不重复实现，避免要 parse JSON）
        """
        out: list[str] = []
        try:
            p = Path(file_path).resolve()
        except (OSError, RuntimeError):
            return out

        # 计算相对路径，仅项目根的写入参与 forbidden_files 拦截
        is_at_root = False
        if project_path:
            try:
                root = Path(project_path).resolve()
                is_at_root = p.parent == root
            except (OSError, RuntimeError):
                is_at_root = False

        # R1: forbidden_files 命中
        if is_at_root and p.name in self.forbidden_files and not p.exists():
            out.append(
                f"charter:build.forbidden_files — refusing to create {p.name} at project root. "
                f"Charter declares build_system={self.build_system or '<unset>'} and "
                f"explicitly forbids {self.forbidden_files}. "
                f"If you really need a different build system, call "
                f"`propose_charter_change` to update charter first; do NOT introduce a "
                f"second build system silently."
            )

        # R2: CMakeLists target rename without charter.artifact.target_name updated
        if p.name == "CMakeLists.txt" and self.target_name and p.exists():
            try:
                old_text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                old_text = ""
            old_targets = set(_CMAKE_TARGET_RE.findall(old_text))
            new_targets = set(_CMAKE_TARGET_RE.findall(new_content or ""))
            if (
                old_targets
                and new_targets
                and self.target_name in old_targets
                and self.target_name not in new_targets
            ):
                out.append(
                    f"charter:artifact.target_name — refusing to rename CMake target away "
                    f"from charter-declared '{self.target_name}'. New content's targets are "
                    f"{sorted(new_targets)}. Either keep '{self.target_name}', or call "
                    f"`propose_charter_change` to update artifact.target_name + "
                    f"deploy.command in the same approved change."
                )

        return out

    # ----------------------------------------------------------
    # 给 system prompt 用的精简版
    # ----------------------------------------------------------

    def summarize_for_prompt(self, max_chars: int = 2000) -> str:
        """返回简洁版 charter 文本注入 LLM system prompt。
        包含：契约关键字段 + body 第一段（如果有）。"""
        lines: list[str] = []
        lines.append("## Project Charter (BINDING — violations are reported as errors)")
        lines.append("")
        lock = "FROZEN — must call `propose_charter_change` to modify" if self.frozen else "mutable"
        lines.append(f"- charter status: **{lock}**")
        if self.project_kind:
            lines.append(f"- project_kind: {self.project_kind}")
        if self.build_system:
            lines.append(f"- build.system: **{self.build_system}** (single-source-of-truth)")
        if self.must_have_files:
            lines.append(f"- build.must_have_files: {self.must_have_files}")
        if self.forbidden_files:
            lines.append(f"- build.forbidden_files: {self.forbidden_files} (writing any of these is a violation)")
        if self.build_command:
            lines.append(f"- build.command: `{self.build_command}`")
        if self.target_name:
            lines.append(f"- artifact.target_name: `{self.target_name}` (renaming requires charter change)")
        if self.artifact.get("output_path"):
            lines.append(f"- artifact.output_path: `{self.artifact['output_path']}`")
        if self.deploy_command:
            lines.append(f"- deploy.command: `{self.deploy_command}`")
        if self.coding_conventions:
            lines.append("- coding_conventions:")
            for c in self.coding_conventions[:8]:
                lines.append(f"    - {c}")
        if self.forbidden_actions:
            lines.append("- forbidden_actions:")
            for a in self.forbidden_actions[:8]:
                lines.append(f"    - {a}")

        lines.append("")
        lines.append(
            "**Rule:** if you believe the charter must change to complete the task, "
            "call `propose_charter_change` (blocking until user approves). Do NOT "
            "introduce changes that violate the charter unilaterally — ProfileGuard "
            "will reject the file_write and the Reviewer will cite `charter:<field>`."
        )

        # body 第一段（200 字符封顶）
        body = (self.body_text or "").strip()
        if body:
            first_para = body.split("\n\n", 1)[0]
            if len(first_para) > 240:
                first_para = first_para[:240].rstrip() + "..."
            lines.append("")
            lines.append("### Charter narrative")
            lines.append(first_para)

        out = "\n".join(lines)
        if len(out) > max_chars:
            out = out[: max_chars - 60] + "\n... (charter summary truncated)"
        return out
