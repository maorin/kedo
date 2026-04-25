"""
Evaluator — 多维度代码质量评估器

评估维度:
  1. requirement_match (0.35) — 需求是否被满足
  2. code_quality     (0.25) — 代码可读性、规范性、复杂度
  3. test_coverage    (0.20) — 测试覆盖率、用例完整性
  4. security         (0.20) — 安全漏洞、危险模式

评估流程:
  静态检查（客观） → LLM 多维度评估（主观） → 合并加权得分
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from api.schemas import CodeChange, EvalDimension, EvalReport, TestResult
from core.memory import AgentMemory

logger = logging.getLogger(__name__)

# 默认维度权重
DEFAULT_DIMENSIONS = [
    {"name": "requirement_match", "weight": 0.35, "label": "需求匹配"},
    {"name": "code_quality",      "weight": 0.25, "label": "代码质量"},
    {"name": "test_coverage",     "weight": 0.20, "label": "测试覆盖"},
    {"name": "security",          "weight": 0.20, "label": "安全性"},
]

EVAL_SYSTEM_PROMPT = """You are a senior code reviewer performing a multi-dimensional evaluation.

CRITICAL SCOPING RULE:
You will be given a "Scoped Requirement" — this is the SPECIFIC sub-task whose output you must score.
You may also be given a "Parent Goal" — this is the wider project goal for context only.
Score `requirement_match` SOLELY against the Scoped Requirement. Do NOT penalize for things that
belong to the Parent Goal but are out of scope for this sub-task. If the Parent Goal mentions X
but the Scoped Requirement does not, X being absent is NOT a missed requirement.

Evaluate the code changes against the Scoped Requirement across these 4 dimensions:

1. **requirement_match** (weight 0.35): Does the code fulfill the Scoped Requirement?
2. **code_quality** (weight 0.25): Is the code clean, readable, well-structured, and following best practices?
3. **test_coverage** (weight 0.20): Are there adequate tests? Are edge cases covered?
4. **security** (weight 0.20): Are there any security vulnerabilities, injection risks, or unsafe patterns?

You will also receive static check results (if available) — factor these into your scoring.

Provide your evaluation as JSON:
{
  "dimensions": [
    {"name": "requirement_match", "score": <0-100>, "details": "..."},
    {"name": "code_quality", "score": <0-100>, "details": "..."},
    {"name": "test_coverage", "score": <0-100>, "details": "..."},
    {"name": "security", "score": <0-100>, "details": "..."}
  ],
  "requirements_met": ["requirement 1 that is satisfied", ...],
  "requirements_missed": ["requirement that is NOT satisfied", ...],
  "risks": ["potential risk or issue", ...],
  "suggestions": ["improvement suggestion", ...]
}

Scoring guide per dimension:
- 90-100: Excellent
- 70-89: Good, minor issues
- 50-69: Acceptable, needs improvement
- Below 50: Needs significant rework
"""


class Evaluator:
    """多维度代码质量评估器"""

    def __init__(self, llm_client, memory: AgentMemory, config: dict = None):
        self._llm = llm_client
        self._memory = memory
        self._config = config or {}
        self._dimensions = self._config.get("eval_dimensions", DEFAULT_DIMENSIONS)
        # 系统提示可被覆盖 — Reviewer Agent 会注入独立裁判风格的 prompt
        self._system_prompt = self._config.get("eval_system_prompt") or EVAL_SYSTEM_PROMPT

    async def evaluate(
        self,
        original_requirement: str,
        code_changes: list[CodeChange],
        test_results: Optional[TestResult] = None,
        project_path: str = ".",
        on_token=None,
        parent_goal: str = "",
        test_strategy: str = "auto",
    ) -> EvalReport:
        """
        多维度评估代码变更

        Args:
            original_requirement: 当前子任务的 scope（评分依据）
            parent_goal: 上层任务/全局需求，仅作为背景上下文，不参与 requirement_match 评分
            test_strategy: 来自 project_profile.test.strategy。值为 "skip" 时表示项目
                无法在主机侧跑测试（交叉编译/嵌入式），test_coverage 维度将从加权中剔除
                并把其权重按比例分摊给其他维度，避免主观分被无谓拉低进 discussion loop。
        """
        # Step 1: 静态检查（客观数据）
        static_checks = await self._run_static_checks(code_changes, project_path)

        # Step 2: LLM 多维度评估（结合静态检查结果）
        llm_report = await self._llm_evaluate(
            original_requirement, code_changes, test_results, static_checks,
            on_token=on_token,
            parent_goal=parent_goal,
        )

        # Step 3: 合并为最终报告
        report = self._merge_report(llm_report, static_checks, test_results, test_strategy=test_strategy)

        # 记录评估结果
        dim_summary = ", ".join(
            f"{d.name}={d.score:.0f}" for d in report.dimensions
        )
        self._memory.add_message(
            "assistant",
            f"Evaluation complete: total={report.score:.0f} [{dim_summary}]",
        )

        return report

    # ----------------------------------------------------------
    # 静态检查
    # ----------------------------------------------------------

    async def _run_static_checks(
        self, code_changes: list[CodeChange], project_path: str
    ) -> dict[str, Any]:
        """运行自动化静态检查，收集客观数据"""
        results: dict[str, Any] = {}

        py_files = [c for c in code_changes if c.file_path.endswith(".py")]

        # Python 语法检查
        if py_files:
            results["python_syntax"] = await self._check_python_syntax(py_files, project_path)

        # Python lint (ruff)
        if py_files:
            results["python_lint"] = await self._check_python_lint(py_files, project_path)

        # 文件大小检查
        results["file_sizes"] = self._check_file_sizes(code_changes)

        # 危险模式检测
        results["dangerous_patterns"] = self._check_dangerous_patterns(code_changes)

        return results

    async def _check_python_syntax(
        self, py_files: list[CodeChange], project_path: str
    ) -> dict:
        """py_compile 语法检查"""
        import asyncio

        errors = []
        for change in py_files:
            filepath = Path(project_path) / change.file_path
            if not filepath.exists():
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python", "-m", "py_compile", str(filepath),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
                if proc.returncode != 0:
                    errors.append({
                        "file": change.file_path,
                        "error": stderr.decode("utf-8", errors="replace").strip(),
                    })
            except Exception as e:
                logger.debug(f"Syntax check failed for {change.file_path}: {e}")

        return {"passed": len(errors) == 0, "errors": errors}

    async def _check_python_lint(
        self, py_files: list[CodeChange], project_path: str
    ) -> dict:
        """ruff lint 检查（如已安装）"""
        import asyncio
        import shutil

        if not shutil.which("ruff"):
            return {"available": False}

        file_paths = []
        for change in py_files:
            fp = Path(project_path) / change.file_path
            if fp.exists():
                file_paths.append(str(fp))

        if not file_paths:
            return {"available": True, "issues": 0}

        try:
            proc = await asyncio.create_subprocess_exec(
                "ruff", "check", "--output-format=json", *file_paths,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=project_path,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            issues = json.loads(stdout.decode()) if stdout.strip() else []
            return {
                "available": True,
                "issues": len(issues),
                "details": issues[:10],  # 最多保留 10 条
            }
        except Exception as e:
            logger.debug(f"Ruff check failed: {e}")
            return {"available": True, "error": str(e)}

    def _check_file_sizes(self, code_changes: list[CodeChange]) -> dict:
        """检查文件大小，标记过大的文件"""
        MAX_LINES = 500
        warnings = []
        for change in code_changes:
            content = change.content or ""
            lines = content.count("\n") + 1 if content else 0
            if lines > MAX_LINES:
                warnings.append({
                    "file": change.file_path,
                    "lines": lines,
                    "warning": f"文件超过 {MAX_LINES} 行，建议拆分",
                })
        return {"warnings": warnings}

    def _check_dangerous_patterns(self, code_changes: list[CodeChange]) -> dict:
        """检测代码中的危险模式"""
        import re

        PATTERNS = [
            (r"eval\s*\(", "eval() 使用", "high"),
            (r"exec\s*\(", "exec() 使用", "high"),
            (r"os\.system\s*\(", "os.system() 使用", "medium"),
            (r"__import__\s*\(", "动态导入", "medium"),
            (r"pickle\.loads?\s*\(", "pickle 反序列化风险", "medium"),
            (r"password\s*=\s*['\"][^'\"]+['\"]", "硬编码密码", "high"),
            (r"api[_-]?key\s*=\s*['\"][^'\"]+['\"]", "硬编码 API Key", "high"),
            (r"innerHTML\s*=", "XSS 风险 (innerHTML)", "high"),
        ]

        findings = []
        for change in code_changes:
            content = change.content or change.diff or ""
            for pattern, description, severity in PATTERNS:
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    findings.append({
                        "file": change.file_path,
                        "pattern": description,
                        "severity": severity,
                        "count": len(matches),
                    })

        return {"findings": findings, "count": len(findings)}

    # ----------------------------------------------------------
    # LLM 评估
    # ----------------------------------------------------------

    async def _llm_evaluate(
        self,
        requirement: str,
        code_changes: list[CodeChange],
        test_results: Optional[TestResult],
        static_checks: dict,
        on_token=None,
        parent_goal: str = "",
    ) -> dict:
        """调用 LLM 进行多维度评估"""
        # 构建代码变更摘要
        changes_str = "\n\n".join(
            f"File: {c.file_path} ({c.action})\n{c.diff or c.content or '(no content)'}"
            for c in code_changes
        )
        # 截断过长的代码
        if len(changes_str) > 30000:
            changes_str = changes_str[:30000] + "\n\n... (truncated)"

        # 构建测试结果信息
        test_info = ""
        if test_results:
            test_info = (
                f"\n\nTest Results:\n"
                f"  Total: {test_results.total}, Passed: {test_results.passed}, "
                f"Failed: {test_results.failed}\n"
                f"  Coverage: {test_results.coverage_percent}%"
            )
            if test_results.failures:
                test_info += "\n  Failures:\n" + "\n".join(
                    f"    - {f['test']}: {f['reason']}" for f in test_results.failures[:5]
                )

        # 构建静态检查信息
        static_info = ""
        if static_checks:
            parts = []
            syntax = static_checks.get("python_syntax", {})
            if syntax:
                status = "PASS" if syntax.get("passed") else "FAIL"
                parts.append(f"Python Syntax: {status}")
                if not syntax.get("passed"):
                    for err in syntax.get("errors", [])[:3]:
                        parts.append(f"  - {err['file']}: {err['error'][:100]}")

            lint = static_checks.get("python_lint", {})
            if lint.get("available"):
                parts.append(f"Lint Issues: {lint.get('issues', 0)}")

            dangerous = static_checks.get("dangerous_patterns", {})
            if dangerous.get("count", 0) > 0:
                patterns = [f"{f['pattern']} in {f['file']} ({f['severity']})"
                            for f in dangerous.get("findings", [])[:5]]
                parts.append(f"Dangerous Patterns: {'; '.join(patterns)}")

            file_sizes = static_checks.get("file_sizes", {})
            if file_sizes.get("warnings"):
                parts.append(f"Large Files: {', '.join(w['file'] for w in file_sizes['warnings'])}")

            if parts:
                static_info = "\n\nStatic Check Results:\n  " + "\n  ".join(parts)

        # parent_goal 仅作为背景上下文呈现，不参与 requirement_match 评分
        parent_section = ""
        if parent_goal and parent_goal.strip() and parent_goal.strip() != requirement.strip():
            parent_section = (
                f"Parent Goal (BACKGROUND ONLY — do NOT score against this):\n"
                f"{parent_goal}\n\n"
            )

        messages = [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": (
                    f"{parent_section}"
                    f"Scoped Requirement (THIS is what you score against):\n{requirement}\n\n"
                    f"Code Changes:\n{changes_str}"
                    f"{test_info}"
                    f"{static_info}\n\n"
                    f"Evaluate these changes across all 4 dimensions, scoring requirement_match "
                    f"ONLY against the Scoped Requirement above. Output JSON."
                ),
            },
        ]

        if on_token and hasattr(self._llm, 'stream_chat'):
            try:
                chunks = []
                async for token in self._llm.stream_chat(messages):
                    chunks.append(token)
                    await on_token(token)
                response = "".join(chunks)
            except Exception as e:
                logger.warning(f"Stream eval failed ({e}), falling back to non-stream")
                response = await self._llm.chat(messages)
        else:
            response = await self._llm.chat(messages)
        return self._parse_llm_report(response)

    def _parse_llm_report(self, response: str) -> dict:
        """解析 LLM 返回的多维度评估 JSON"""
        try:
            if "```" in response:
                json_str = response.split("```")[1]
                if json_str.startswith("json"):
                    json_str = json_str[4:]
                json_str = json_str.strip()
            else:
                start = response.find("{")
                end = response.rfind("}") + 1
                json_str = response[start:end]

            return json.loads(json_str)
        except Exception as e:
            logger.error(f"Failed to parse eval report: {e}")
            return {}

    # ----------------------------------------------------------
    # 合并报告
    # ----------------------------------------------------------

    def _merge_report(
        self,
        llm_report: dict,
        static_checks: dict,
        test_results: Optional[TestResult],
        test_strategy: str = "auto",
    ) -> EvalReport:
        """合并 LLM 评估和静态检查为最终报告"""
        # 构建维度列表
        dimensions = []
        llm_dims = {d["name"]: d for d in llm_report.get("dimensions", [])}

        # profile 声明 test.strategy=skip 时（交叉编译/嵌入式项目），test_coverage
        # 维度无意义，从加权中剔除并把其权重按比例分摊到其他维度。
        active_dims = list(self._dimensions)
        if test_strategy == "skip":
            dropped = [d for d in active_dims if d["name"] == "test_coverage"]
            active_dims = [d for d in active_dims if d["name"] != "test_coverage"]
            if dropped and active_dims:
                dropped_weight = dropped[0]["weight"]
                total_remaining = sum(d["weight"] for d in active_dims)
                if total_remaining > 0:
                    active_dims = [
                        {**d, "weight": d["weight"] + dropped_weight * (d["weight"] / total_remaining)}
                        for d in active_dims
                    ]

        for dim_config in active_dims:
            name = dim_config["name"]
            weight = dim_config["weight"]
            llm_dim = llm_dims.get(name, {})

            # 默认 75：与 LLM 打分中位数对齐，避免 LLM 漏报某维度时被默认 60 无谓拉低总分
            score = float(llm_dim.get("score", 75))
            details = llm_dim.get("details", "")

            # 用静态检查结果修正分数
            score, details = self._adjust_score_with_static(
                name, score, details, static_checks, test_results
            )

            dimensions.append(EvalDimension(
                name=name,
                score=score,
                weight=weight,
                details=details,
            ))

        # 计算加权总分
        total_score = sum(d.score * d.weight for d in dimensions)

        return EvalReport(
            score=round(total_score, 1),
            dimensions=dimensions,
            requirements_met=llm_report.get("requirements_met", []),
            requirements_missed=llm_report.get("requirements_missed", []),
            risks=llm_report.get("risks", []),
            suggestions=llm_report.get("suggestions", []),
            static_checks=static_checks,
        )

    def _adjust_score_with_static(
        self,
        dimension: str,
        score: float,
        details: str,
        static_checks: dict,
        test_results: Optional[TestResult],
    ) -> tuple[float, str]:
        """用静态检查的客观数据修正 LLM 主观评分"""

        if dimension == "code_quality":
            # 语法错误 → 直接扣分
            syntax = static_checks.get("python_syntax", {})
            if syntax and not syntax.get("passed", True):
                error_count = len(syntax.get("errors", []))
                penalty = min(30, error_count * 15)
                score = max(0, score - penalty)
                details += f" [语法错误 -{penalty}分]"

            # lint 问题 → 按数量扣分
            lint = static_checks.get("python_lint", {})
            if lint.get("available") and lint.get("issues", 0) > 0:
                issue_count = lint["issues"]
                penalty = min(20, issue_count * 2)
                score = max(0, score - penalty)
                details += f" [lint {issue_count}个问题 -{penalty}分]"

        elif dimension == "test_coverage":
            # 用实际覆盖率数据修正
            if test_results and test_results.total > 0:
                coverage = test_results.coverage_percent
                pass_rate = (test_results.passed / test_results.total * 100) if test_results.total else 0
                # 加权：覆盖率占 60%，通过率占 40%
                objective_score = coverage * 0.6 + pass_rate * 0.4
                # LLM 主观评分和客观数据各占一半
                score = (score + objective_score) / 2
                details += f" [覆盖率{coverage:.0f}%, 通过率{pass_rate:.0f}%]"
            elif test_results and test_results.total == 0:
                # 无测试，上限 40 分
                score = min(score, 40)
                details += " [无测试用例]"

        elif dimension == "security":
            # 危险模式 → 直接扣分
            dangerous = static_checks.get("dangerous_patterns", {})
            findings = dangerous.get("findings", [])
            high_count = sum(1 for f in findings if f.get("severity") == "high")
            medium_count = sum(1 for f in findings if f.get("severity") == "medium")
            if high_count > 0:
                penalty = min(40, high_count * 15)
                score = max(0, score - penalty)
                details += f" [高危模式 {high_count}个 -{penalty}分]"
            if medium_count > 0:
                penalty = min(15, medium_count * 5)
                score = max(0, score - penalty)
                details += f" [中危模式 {medium_count}个 -{penalty}分]"

        return round(score, 1), details
