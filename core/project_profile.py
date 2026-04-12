"""
ProjectProfileManager — LLM 驱动的项目能力档案

为每个项目生成并持久化一份"profile"，描述：
  - 项目类型 (switch_homebrew / android_native / cpp_host / ...)
  - 构建命令和产物
  - 测试策略（关键：交叉编译类项目可声明 test.strategy=skip）
  - 必需的环境变量及典型安装路径
  - 部署方式（可选）

设计理念（kedo 改进计划方案 Z）：
  - 把"懂 toolchain"这件事的存储位置从代码迁移到 LLM context + 落盘 profile
  - 不维护 toolchain 注册表，新项目类型靠 LLM 推断 0 成本支持
  - profile 一旦生成就 freeze 到 .kedo/project_profile.json，下次直接读取
  - manifest 文件 hash 变化时自动失效重新生成
  - LLM profile 失败 → 用户可手工编辑 JSON 作为 W 兜底

profile 文件示例（switchvideo）：
{
  "version": 1,
  "type": "switch_homebrew",
  "build": {
    "command": "mkdir -p build && cd build && cmake -DCMAKE_TOOLCHAIN_FILE=$DEVKITPRO/cmake/Switch.cmake .. && make -j$(nproc)",
    "working_dir": ".",
    "artifacts": ["build/*.nro"]
  },
  "test": {
    "strategy": "skip",
    "reason": "Cross-compiled to aarch64-none-elf; host cannot execute test binaries",
    "command": null
  },
  "required_env": [
    {"name": "DEVKITPRO", "search_paths": ["/opt/devkitpro", "/usr/local/devkitpro", "~/devkitpro"], "verify_file": "cmake/Switch.cmake"}
  ],
  "notes": "Use elf2nro POST_BUILD step to package the .nro artifact",
  "generated_by": "llm",
  "human_verified": false,
  "manifest_fingerprint": "sha256:...",
  "fail_count": 0
}
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROFILE_VERSION = 1
PROFILE_FILE = ".kedo/project_profile.json"

# 构建清单文件——按重要度顺序读取，截断到合理大小喂 LLM
MANIFEST_FILES = [
    "CMakeLists.txt", "Makefile", "makefile", "GNUmakefile",
    "build.gradle", "build.gradle.kts", "settings.gradle",
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "BUILD.bazel", "BUILD", "WORKSPACE",
    "platformio.ini", "Pipfile", "requirements.txt",
    "Dockerfile", "flake.nix", "shell.nix", "devbox.json",
]

MAX_MANIFEST_BYTES = 6000
MAX_MANIFESTS_TO_INCLUDE = 6

# prior_attempts 列表上限：只保留最近 N 次失败快照给 LLM 作为 negative examples
MAX_PRIOR_ATTEMPTS = 5
# 单条 prior_attempt 中 stderr 摘要的字节上限
PRIOR_ATTEMPT_STDERR_LIMIT = 1500

PROFILE_SYSTEM_PROMPT = """You are a build system expert embedded in an AI development pipeline.
Given the manifest files of a software project, output a JSON profile describing
how to build, test, and deploy it.

CRITICAL RULES:

1. **Cross-compile awareness**: If the project is cross-compiled and the host
   CANNOT execute its outputs (e.g., Nintendo Switch homebrew, embedded firmware,
   kernel modules, ESP-IDF, ARM bare-metal), you MUST set test.strategy="skip"
   with a clear reason. Do NOT propose host-side test commands for such projects.

2. **Build commands must be self-contained shell snippets**. Use $ENV_VAR for
   environment variables that the host must provide. Pick a working directory
   relative to project root.

   **Build commands MUST start by cleaning any stale build artifacts** (e.g.
   `rm -rf build && mkdir -p build && ...` for CMake projects). This is
   non-negotiable: a stale CMake cache will silently ignore a newly passed
   `-DCMAKE_TOOLCHAIN_FILE` and fall back to the host compiler, which is
   the most common source of cross-compilation build failures. Even for
   non-CMake projects, prefer commands that produce clean output.

3. **Required env**: List every environment variable the build command depends on,
   along with hint paths where the toolchain is typically installed. Include a
   small "verify_file" relative to the env-var path so the host can confirm the
   tool is actually installed there.

4. **Test strategy must be one of**:
   - "skip"      : do not run any tests (use this for cross-compile targets)
   - "ctest"     : run via ctest in build directory
   - "pytest"    : run pytest
   - "go_test"   : run go test ./...
   - "cargo_test": run cargo test
   - "npm_test"  : run npm test
   - "custom"    : provide a custom command in test.command

5. **Be specific about paths and commands**. No placeholders like "<your_command_here>".

6. **Deploy** (optional but recommended for projects targeting external devices):
   If the project produces an artifact that needs to be transferred to an external
   device (Switch SD card, Android device, embedded board, etc.), include a `deploy`
   field with the shell command. The command runs after a successful build. Leave
   the field out for projects that don't have an external deploy target (e.g. host
   libraries, host CLI tools).

OUTPUT STRICT JSON (no markdown, no commentary), exactly this shape:
{
  "type": "<concise project type, e.g. switch_homebrew, android_native, cpp_host, rust_embedded>",
  "build": {
    "command": "<full shell command>",
    "working_dir": ".",
    "artifacts": ["<glob patterns relative to project root>"]
  },
  "test": {
    "strategy": "skip" | "ctest" | "pytest" | "go_test" | "cargo_test" | "npm_test" | "custom",
    "reason": "<one sentence explaining why this strategy>",
    "command": "<shell command, or null if strategy=skip>"
  },
  "deploy": {
    "method": "<short label, e.g. switch_dbi_mtp, adb_push, scp, none>",
    "command": "<full shell command, or null if method=none>",
    "notes": "<one sentence: prerequisites or known quirks>"
  },
  "required_env": [
    {"name": "<ENV_VAR>", "search_paths": ["<abs path 1>", "<abs path 2>"], "verify_file": "<rel path inside the toolchain root>"}
  ],
  "notes": "<anything else humans should know about building/testing this project>"
}
"""


class ProjectProfile(dict):
    """Profile 是个 dict，加了几个语义访问器。"""

    @property
    def build_command(self) -> str:
        return ((self.get("build") or {}).get("command")) or ""

    @property
    def test_strategy(self) -> str:
        return ((self.get("test") or {}).get("strategy")) or "auto"

    @property
    def test_command(self) -> Optional[str]:
        return (self.get("test") or {}).get("command")

    @property
    def test_reason(self) -> str:
        return (self.get("test") or {}).get("reason") or ""

    @property
    def required_env(self) -> list[dict]:
        return self.get("required_env") or []

    @property
    def fail_count(self) -> int:
        return int(self.get("fail_count") or 0)

    @property
    def total_regens(self) -> int:
        """该项目历史上 profile 被 LLM 重新生成过多少次（跨 regen 累积）。"""
        return int(self.get("total_regens") or 0)

    @property
    def prior_attempts(self) -> list[dict]:
        """
        历史失败的 profile 尝试快照，格式：
          [{"version": int, "build_command": str, "stderr_excerpt": str, "failed_at": iso}, ...]
        跨 regen 持久化，作为 LLM 重生成时的 negative examples，避免在相近错误之间反复跳。
        """
        return self.get("prior_attempts") or []

    @property
    def human_verified(self) -> bool:
        return bool(self.get("human_verified"))


class ProjectProfileManager:
    """
    管理项目能力 profile 的生成、加载、持久化、失效。

    集成方式（agent_loop 调用）：
        profile = await pm.ensure(project_path, llm_client)
        if profile:
            build_cmd = profile.build_command
            ...
    """

    def __init__(self):
        self._cache: dict[str, ProjectProfile] = {}

    # ----------------------------------------------------------
    # 加载/保存
    # ----------------------------------------------------------

    def _profile_path(self, project_path: str) -> Path:
        return Path(project_path) / PROFILE_FILE

    def load(self, project_path: str) -> Optional[ProjectProfile]:
        """从磁盘读 profile，找不到/损坏返回 None。"""
        cached = self._cache.get(project_path)
        if cached is not None:
            return cached
        path = self._profile_path(project_path)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"ProjectProfile: failed to read {path}: {e}")
            return None
        if not isinstance(data, dict):
            return None
        profile = ProjectProfile(data)
        self._cache[project_path] = profile
        return profile

    def save(self, project_path: str, profile: ProjectProfile) -> None:
        path = self._profile_path(project_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        profile["updated_at"] = datetime.utcnow().isoformat()
        path.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
        self._cache[project_path] = profile
        logger.info(f"ProjectProfile saved: type={profile.get('type')} → {path}")

    def invalidate(self, project_path: str) -> None:
        self._cache.pop(project_path, None)

    # ----------------------------------------------------------
    # 生成 / 失效检测
    # ----------------------------------------------------------

    def _read_manifests(self, project_path: str) -> list[tuple[str, str]]:
        """读取项目根目录下的构建 manifest 文件。"""
        proj = Path(project_path)
        results: list[tuple[str, str]] = []
        for name in MANIFEST_FILES:
            f = proj / name
            if f.exists() and f.is_file():
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    if len(text) > MAX_MANIFEST_BYTES:
                        text = text[:MAX_MANIFEST_BYTES] + "\n... (truncated)"
                    results.append((name, text))
                    if len(results) >= MAX_MANIFESTS_TO_INCLUDE:
                        break
                except Exception as e:
                    logger.debug(f"manifest read failed {name}: {e}")
        return results

    def _fingerprint(self, manifests: list[tuple[str, str]]) -> str:
        """对 manifest 内容做 SHA256 指纹，用于检测 staleness。"""
        h = hashlib.sha256()
        for name, content in sorted(manifests):
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            h.update(content.encode("utf-8"))
            h.update(b"\0")
        return "sha256:" + h.hexdigest()[:32]

    def is_stale(self, profile: ProjectProfile, project_path: str) -> bool:
        """判断 profile 是否过期（manifest 文件有变化）。"""
        if profile.human_verified:
            return False  # 人工验证过的 profile 永不自动失效
        manifests = self._read_manifests(project_path)
        current_fp = self._fingerprint(manifests)
        return profile.get("manifest_fingerprint") != current_fp

    async def ensure(
        self,
        project_path: str,
        llm_client,
        force_regenerate: bool = False,
        previous_error: Optional[str] = None,
    ) -> Optional[ProjectProfile]:
        """
        加载或生成 profile。

        - 已存在且未过期 → 直接返回（0 LLM 调用）
        - 不存在或 stale → 调 LLM 生成
        - force_regenerate=True → 强制重新生成（可带 previous_error 作为修复 hint）
        - LLM 失败 → 返回 None，调用方应回退到原推断逻辑
        """
        # 先读一次旧 profile —— 即使要 regen 也需要它的 total_regens / prior_attempts
        # 作为"跨 regen 持久化"的来源
        existing = self.load(project_path)
        if not force_regenerate:
            if existing and not self.is_stale(existing, project_path):
                return existing

        manifests = self._read_manifests(project_path)
        if not manifests:
            logger.info(f"ProjectProfile: no manifests found in {project_path}, skip generation")
            return None

        # 跨 regen 持久化的字段：旧 profile 的失败历史要传给 LLM 作 negative examples
        carried_prior_attempts: list[dict] = []
        carried_total_regens = 0
        if existing is not None:
            carried_prior_attempts = list(existing.prior_attempts)
            carried_total_regens = existing.total_regens

        try:
            profile = await self._generate_via_llm(
                llm_client,
                manifests,
                previous_error=previous_error,
                prior_attempts=carried_prior_attempts,
            )
        except Exception as e:
            logger.warning(f"ProjectProfile generation failed: {e}")
            return None

        if profile is None:
            return None

        profile["manifest_fingerprint"] = self._fingerprint(manifests)
        profile["generated_by"] = "llm"
        profile["human_verified"] = False
        profile["fail_count"] = 0  # 当前版本计数归零
        # 以下两个字段跨 regen 持久化：fail_count 可以清零，但"这项目改了几版 profile"
        # 和"历史失败快照"必须保留，用于上限保护 + 下次 regen 的 negative examples
        if force_regenerate and existing is not None:
            profile["total_regens"] = carried_total_regens + 1
        else:
            profile["total_regens"] = carried_total_regens
        profile["prior_attempts"] = carried_prior_attempts
        profile.setdefault("version", PROFILE_VERSION)
        profile.setdefault("created_at", datetime.utcnow().isoformat())
        self.save(project_path, profile)
        return profile

    async def _generate_via_llm(
        self,
        llm_client,
        manifests: list[tuple[str, str]],
        previous_error: Optional[str] = None,
        prior_attempts: Optional[list[dict]] = None,
    ) -> Optional[ProjectProfile]:
        files_block = "\n\n".join(f"=== {name} ===\n{content}" for name, content in manifests)
        user_content = (
            f"Project manifest files:\n\n{files_block}\n\n"
            f"Output the JSON profile."
        )
        # 跨 regen 的 negative examples：告诉 LLM 历史上试过哪些 build_command 并且都挂了，
        # 不要在相近变体之间反复跳
        if prior_attempts:
            neg_block_lines = []
            for i, att in enumerate(prior_attempts, 1):
                neg_block_lines.append(
                    f"  [attempt {i}, profile v{att.get('version', '?')}]\n"
                    f"    build_command: {att.get('build_command', '')[:400]}\n"
                    f"    stderr (tail): {att.get('stderr_excerpt', '')[:800]}"
                )
            user_content = (
                f"IMPORTANT: {len(prior_attempts)} previous profile(s) have already been "
                f"tried on this project and ALL failed. Here are the failed attempts — "
                f"DO NOT produce a build_command that is structurally similar to any of "
                f"them, and explicitly avoid repeating the same mistakes:\n\n"
                f"{chr(10).join(neg_block_lines)}\n\n"
                f"{user_content}"
            )
        if previous_error:
            user_content = (
                f"A previously generated profile caused a build failure. Use this "
                f"failure context to produce a CORRECTED profile — do not repeat the "
                f"same mistake.\n\n"
                f"Build failure stderr:\n```\n{previous_error[:3000]}\n```\n\n"
                f"Common pitfalls based on this kind of error:\n"
                f"  - **Stale build/ cache**: failing to `rm -rf build` before "
                f"re-running cmake. CMake will use the previously cached toolchain "
                f"setting and ignore -DCMAKE_TOOLCHAIN_FILE. ALWAYS start the build "
                f"command with `rm -rf build && mkdir -p build && cd build && ...`\n"
                f"  - Wrong CMake toolchain file path (e.g. $DEVKITPRO/switch.cmake "
                f"or $DEVKITPRO/devkitA64.cmake — both wrong. The correct path is "
                f"$DEVKITPRO/cmake/Switch.cmake — note capital S and the cmake/ "
                f"subdirectory)\n"
                f"  - Missing -DCMAKE_TOOLCHAIN_FILE flag entirely, causing host "
                f"compiler to be picked\n"
                f"  - Wrong working directory or missing mkdir step\n"
                f"  - Forgetting elf2nro / packaging post-build for Switch homebrew\n\n"
                f"{user_content}"
            )
        messages = [
            {"role": "system", "content": PROFILE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        response = await llm_client.chat(messages)
        return self._parse_response(response)

    def _parse_response(self, response: str) -> Optional[ProjectProfile]:
        if not response:
            return None
        text = response.strip()
        # 去掉 ```json 包裹
        if "```" in text:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if m:
                text = m.group(1).strip()
        # 取首个完整 JSON 对象
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        end = -1
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
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
        except Exception as e:
            logger.warning(f"ProjectProfile: parse failed: {e}")
            return None
        if not isinstance(data, dict):
            return None
        # 最小 schema 校验
        if "build" not in data or not isinstance(data.get("build"), dict):
            logger.warning("ProjectProfile: missing 'build' object")
            return None
        if not data["build"].get("command"):
            logger.warning("ProjectProfile: build.command is empty")
            return None
        if "test" not in data or not isinstance(data.get("test"), dict):
            data["test"] = {"strategy": "skip", "reason": "no test strategy from LLM", "command": None}
        return ProjectProfile(data)

    # ----------------------------------------------------------
    # 失败计数 + W 兜底
    # ----------------------------------------------------------

    def mark_failure(
        self,
        project_path: str,
        stderr: Optional[str] = None,
        build_command: Optional[str] = None,
    ) -> int:
        """
        记录一次基于 profile 的 build 失败，返回当前版本累计失败次数。

        当传入 stderr / build_command 时，同时把本次失败快照追加到 prior_attempts
        列表里（跨 regen 持久化）。这样下一次 LLM 重生成 profile 时就能看到
        "之前的 build_command 是 X，挂在 stderr Y" 作为 negative examples，避免
        在相近错误之间反复跳。
        """
        profile = self.load(project_path)
        if profile is None:
            return 0
        profile["fail_count"] = profile.fail_count + 1

        if stderr or build_command:
            attempts = list(profile.prior_attempts)
            attempts.append({
                "version": profile.total_regens + 1,  # 第几代 profile
                "build_command": (build_command or profile.build_command or "")[:500],
                "stderr_excerpt": (stderr or "")[-PRIOR_ATTEMPT_STDERR_LIMIT:],
                "failed_at": datetime.utcnow().isoformat(),
            })
            # 只保留最近 MAX_PRIOR_ATTEMPTS 条
            profile["prior_attempts"] = attempts[-MAX_PRIOR_ATTEMPTS:]

        self.save(project_path, profile)
        return profile["fail_count"]

    def reset_failures(self, project_path: str) -> None:
        profile = self.load(project_path)
        if profile is None:
            return
        if profile.fail_count > 0:
            profile["fail_count"] = 0
            self.save(project_path, profile)

    # ----------------------------------------------------------
    # required_env 主动探测
    # ----------------------------------------------------------

    def apply_required_env(self, profile: ProjectProfile) -> dict[str, str]:
        """
        遍历 profile.required_env，对每个未设置的环境变量主动探测安装路径。
        命中即写入 os.environ，返回新设置的 {var: path} 字典。
        """
        applied: dict[str, str] = {}
        for entry in profile.required_env:
            name = entry.get("name")
            if not name or os.environ.get(name):
                continue
            verify = entry.get("verify_file") or ""
            for raw in entry.get("search_paths") or []:
                p = Path(os.path.expanduser(raw))
                if not p.exists():
                    continue
                if verify and not (p / verify).exists():
                    continue
                os.environ[name] = str(p)
                applied[name] = str(p)
                logger.info(f"ProjectProfile: auto-set {name}={p}")
                # 衍生路径：DEVKITPRO 特殊处理（兼容 _auto_fix_build_env 的逻辑）
                if name == "DEVKITPRO":
                    devkita64 = p / "devkitA64"
                    if devkita64.exists() and not os.environ.get("DEVKITA64"):
                        os.environ["DEVKITA64"] = str(devkita64)
                    cur_path = os.environ.get("PATH", "")
                    for bin_dir in (p / "tools" / "bin", devkita64 / "bin"):
                        if bin_dir.exists() and str(bin_dir) not in cur_path:
                            cur_path = str(bin_dir) + ":" + cur_path
                    os.environ["PATH"] = cur_path
                break
        return applied
