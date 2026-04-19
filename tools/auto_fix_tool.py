"""
AutoFixTool — 把 AgentLoop._attempt_llm_fix 工具化

LLM-driven 单轮修复：诊断 stderr → 输出 patch → 写文件（受 ProfileGuard 保护）。
循环逻辑放在 ReactAgent 主循环里：build fail → call auto_fix → 再 build → ...

不实现 retry-with-prior-attempts 的 N-attempt 循环 —— 那是 ReactAgent 的事；
本工具每次调用做"一次诊断 + 一次写"。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


_FIX_CONTEXT_BUILD_MANIFESTS = [
    "CMakeLists.txt", "Makefile", "makefile",
    "build.gradle", "build.gradle.kts", "package.json",
    "pyproject.toml", "setup.py", "Cargo.toml", "go.mod",
    "BUILD.bazel", "BUILD",
]


class AutoFixTool(BaseTool):
    def __init__(self, llm_client, profile_manager=None, profile_guard=None):
        self._llm = llm_client
        self._profile_mgr = profile_manager
        self._guard = profile_guard

    @property
    def name(self) -> str:
        return "auto_fix"

    @property
    def description(self) -> str:
        return (
            "LLM-driven single-shot fix for a build/test failure. Pass the failed step "
            "description and the error output; the tool will analyze relevant project "
            "files (CMakeLists, Makefile, target source) + recent failed attempts, "
            "diagnose the root cause, and apply ONE file edit. Returns the diagnosis "
            "and the file that was changed. Caller (you) should re-run build/test to "
            "verify, then optionally call auto_fix again for the next error."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("failed_step", "string",
                          "Short description of what failed (e.g., 'build', 'compile main.c')"),
            ToolParameter("error_text", "string",
                          "Full error output from the failed step (stderr/log)"),
            ToolParameter("target_file_hint", "string",
                          "Hint: relative path of the file you suspect needs fixing (optional)",
                          required=False),
            ToolParameter("project_path", "string",
                          "Project root (auto-injected by ReactAgent)",
                          required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        return False  # 会写文件

    async def execute(
        self,
        failed_step: str,
        error_text: str,
        target_file_hint: str = "",
        project_path: str = ".",
    ) -> ToolResult:
        proj = Path(project_path).resolve()
        if not proj.exists():
            return ToolResult(success=False, error=f"project_path not found: {project_path}")

        # 1) 收集上下文文件
        context_files = self._gather_context(proj, target_file_hint)

        # 2) 注入 prior_attempts
        prior_block = ""
        if self._profile_mgr:
            try:
                profile = self._profile_mgr.load(str(proj))
                if profile and profile.prior_attempts:
                    recent = profile.prior_attempts[-3:]
                    lines = ["PREVIOUS FAILED ATTEMPTS (avoid repeating these fixes):"]
                    for a in recent:
                        lines.append(
                            f"  - build_command: {(a.get('build_command') or '')[:200]}\n"
                            f"    stderr: {(a.get('stderr_excerpt') or '')[:300]}"
                        )
                    prior_block = "\n".join(lines) + "\n\n"
            except Exception as e:
                logger.debug(f"AutoFixTool: prior_attempts load failed: {e}")

        # 3) 构 LLM prompt
        messages = self._build_prompt(failed_step, error_text, context_files, prior_block)

        # 4) 调 LLM（用本工具持有的 llm_client，不复用 ReactAgent 的；行为一致）
        try:
            response = await self._llm.chat(messages)
        except Exception as e:
            return ToolResult(success=False, error=f"auto_fix LLM call failed: {e}")

        # 5) 解析
        patch = self._parse_response(response)
        if patch is None:
            return ToolResult(
                success=False,
                error="auto_fix LLM response not parseable as fix patch JSON",
                data={"raw_response_excerpt": (response or "")[:500]},
            )
        if patch.get("unfixable"):
            reason = patch.get("reason", "")
            return ToolResult(
                success=False,
                error=f"LLM declared unfixable: {reason}",
                data={"unfixable": True, "reason": reason},
            )

        # 6) 应用补丁（带 ProfileGuard 拦截）
        rel = patch["file_to_fix"].lstrip("/").replace("\\", "/")
        target = proj / rel
        new_content = patch["new_content"]
        diagnosis = (patch.get("diagnosis") or "")[:300]

        if self._guard:
            violation = self._guard.check(str(target), new_content)
            if violation:
                logger.warning(f"AutoFixTool blocked by ProfileGuard: {violation[:120]}")
                return ToolResult(
                    success=False,
                    error=f"Patch rejected by ProfileGuard: {violation}",
                    data={"diagnosis": diagnosis, "blocked_file": rel},
                )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return ToolResult(success=False, error=f"failed to write {target}: {e}")

        # 7) 记录 prior_attempt（让下次 auto_fix 看到这次没生效就不要再试同样思路）
        if self._profile_mgr:
            try:
                profile = self._profile_mgr.load(str(proj))
                if profile is not None:
                    attempts = list(profile.get("prior_attempts") or [])
                    attempts.append({
                        "build_command": failed_step[:200],
                        "stderr_excerpt": (error_text or "")[:500],
                        "patched_file": rel,
                        "diagnosis": diagnosis,
                    })
                    profile["prior_attempts"] = attempts[-10:]  # 保留最近 10 条
                    self._profile_mgr.save(str(proj), profile)
            except Exception as e:
                logger.debug(f"AutoFixTool: prior_attempt save failed: {e}")

        return ToolResult(
            success=True,
            output=f"Patched {rel}: {diagnosis}",
            data={"file_path": rel, "diagnosis": diagnosis, "action": patch.get("action", "modify")},
        )

    # --------------------------------------------------------------
    # 上下文采集
    # --------------------------------------------------------------

    @staticmethod
    def _gather_context(proj: Path, target_file_hint: str) -> list[tuple[str, str]]:
        """采集与失败相关的项目文件 → [(rel_path, content)]，单文件 8KB 截断"""
        max_per_file = 8192
        results: list[tuple[str, str]] = []
        seen: set[str] = set()

        # profile.json (可能含 build/test 命令信息)
        profile_path = proj / ".kedo" / "project_profile.json"
        if profile_path.is_file():
            try:
                content = profile_path.read_text(encoding="utf-8", errors="replace")
                results.append((".kedo/project_profile.json", content[:max_per_file]))
                seen.add(".kedo/project_profile.json")
            except Exception:
                pass

        # 构建清单
        for name in _FIX_CONTEXT_BUILD_MANIFESTS:
            if name in seen:
                continue
            f = proj / name
            if f.is_file():
                try:
                    results.append((name, f.read_text(encoding="utf-8", errors="replace")[:max_per_file]))
                    seen.add(name)
                except Exception:
                    pass

        # 用户提示的目标文件
        if target_file_hint:
            hint_rel = target_file_hint.lstrip("/").replace("\\", "/")
            if hint_rel not in seen:
                hint_path = proj / hint_rel
                if hint_path.is_file():
                    try:
                        results.append(
                            (hint_rel, hint_path.read_text(encoding="utf-8", errors="replace")[:max_per_file])
                        )
                        seen.add(hint_rel)
                    except Exception:
                        pass

        return results

    # --------------------------------------------------------------
    # Prompt 构建
    # --------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        failed_step: str,
        error_text: str,
        context_files: list[tuple[str, str]],
        prior_block: str,
    ) -> list[dict]:
        files_block = "\n\n".join(
            f"=== {path} ===\n{content}" for path, content in context_files
        ) or "(no relevant files found in project root)"

        system = (
            "You are a senior build/test debugging expert embedded in an automated software "
            "engineering pipeline. A build/test step has just failed. Your job: diagnose the "
            "root cause and propose ONE concrete file change that will let the step succeed.\n\n"
            "RULES:\n"
            "1. Read the error output and the project files carefully before deciding.\n"
            "2. If a single file edit can fix the failure, output the COMPLETE new content "
            "of that file (not a diff or partial snippet).\n"
            "3. NEVER remove load-bearing flags from build commands "
            "(-DCMAKE_TOOLCHAIN_FILE, -specs=, -lnx, etc).\n"
            "4. NEVER drop critical Makefile targets (all/build/clean) or critical CMake calls "
            "(cmake_minimum_required/project/add_executable) — the ProfileGuard will reject "
            "such writes anyway.\n"
            "5. Prefer minimal, targeted edits over rewrites.\n"
            "6. Use forward slashes in file paths, RELATIVE TO PROJECT ROOT.\n"
            "7. Fix ONLY the first/most-causal error — the build will be re-run after.\n"
            "8. If the failure is structural (wrong tool, missing system package, plan-level "
            "wrong) and CANNOT be fixed by a single file edit, return the unfixable schema.\n\n"
            "Output STRICTLY one of these two JSON shapes (no markdown fences, no commentary):\n"
            '{\n'
            '  "diagnosis": "<one-sentence root cause>",\n'
            '  "file_to_fix": "<relative/path>",\n'
            '  "action": "create" | "modify",\n'
            '  "new_content": "<complete new file content>"\n'
            '}\n'
            "OR\n"
            '{"unfixable": true, "reason": "<why a single-file edit cannot fix this>"}'
        )

        user = (
            f"Failed step: {failed_step}\n\n"
            f"{prior_block}"
            f"Error output:\n{(error_text or '')[:4000]}\n\n"
            f"Relevant project files:\n{files_block}\n\n"
            f"Diagnose and respond with the JSON."
        )

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    # --------------------------------------------------------------
    # 响应解析
    # --------------------------------------------------------------

    @staticmethod
    def _parse_response(response: str) -> Optional[dict]:
        """解析 LLM 修复响应 JSON。兼容 markdown 包裹 + 取首个完整对象。"""
        if not response:
            return None
        text = response.strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if m:
                text = m.group(1).strip()
        # 找第一个完整 JSON 对象
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        end = -1
        in_str = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if in_str:
                if c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            return None
        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            logger.warning(f"AutoFixTool: JSON decode failed: {e}")
            return None
        if data.get("unfixable"):
            return {"unfixable": True, "reason": data.get("reason", "")}
        if not all(k in data for k in ("file_to_fix", "new_content")):
            return None
        return data
