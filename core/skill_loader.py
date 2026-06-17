"""
SkillLoader — kedo 消费本地 Agent Skill 包（Skill 双向 · 方向 1）

兼容 Claude Code / Codex 通用的 Agent Skill 格式：一个目录 + `SKILL.md`
（YAML frontmatter `name`/`description` + markdown 指令正文），可选随包
`scripts/`、`references/` 等。kedo 把已安装 skill 的「名字 + 描述」做成目录
注入 ReactAgent 系统 prompt，正文/随包文件由 agent 按需用 skill_read /
file_read 读取，再用现有 shell_execute / git / browser_* 工具执行。

安装位置：~/.config/kedo/skills/<name>/（全局，跨项目复用）。
install 只做 git clone / 本地拷贝 + 解析 frontmatter，**不执行**包里的任何脚本
——脚本只在 agent 遵循 skill 时经 shell_execute 跑（与 Claude Code skill 同信任模型）。

详见 docs/roadmap.md 的「Skill 双向」段。复用了 core/project_charter.py 的
frontmatter 解析范式。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# YAML frontmatter 边界（与 Charter 同正则）
_FRONTMATTER_RE = re.compile(
    r"^---\s*\r?\n(?P<yaml>.*?)\r?\n---\s*\r?\n?(?P<body>.*)$",
    re.DOTALL,
)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def default_skills_dir() -> Path:
    return Path.home() / ".config" / "kedo" / "skills"


@dataclass
class Skill:
    name: str
    description: str = ""
    body: str = ""                       # SKILL.md 正文（去掉 frontmatter）
    dir: str = ""                        # skill 目录绝对路径
    files: list = field(default_factory=list)  # 随包文件相对路径（不含 .git / SKILL.md）
    source: str = ""                     # 安装来源（git url / 本地路径）
    raw_yaml: dict = field(default_factory=dict)

    def to_public(self, with_body: bool = False) -> dict:
        d = {
            "name": self.name,
            "description": self.description,
            "dir": self.dir,
            "files": self.files,
            "source": self.source,
        }
        if with_body:
            d["body"] = self.body
        return d


class SkillLoader:
    def __init__(self, skills_dir: Optional[str] = None):
        self.skills_dir = Path(skills_dir) if skills_dir else default_skills_dir()
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- 解析
    @staticmethod
    def _safe_name(name: str) -> str:
        n = _SAFE_NAME_RE.sub("-", (name or "").strip()).strip("-.")
        return n or "skill"

    @classmethod
    def _parse_skill_md(cls, skill_md: Path) -> Optional[dict]:
        """解析 SKILL.md → {name, description, body, raw}；无 frontmatter 时降级用目录名。"""
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SkillLoader: read failed {skill_md}: {e}")
            return None
        m = _FRONTMATTER_RE.match(text)
        raw, body = {}, text
        if m:
            body = m.group("body") or ""
            try:
                import yaml
                raw = yaml.safe_load(m.group("yaml")) or {}
                if not isinstance(raw, dict):
                    raw = {}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"SkillLoader: frontmatter YAML parse failed {skill_md}: {e}")
                raw = {}
        name = str(raw.get("name") or skill_md.parent.name).strip()
        return {
            "name": name,
            "description": str(raw.get("description") or "").strip(),
            "body": body,
            "raw": raw,
        }

    def _load_dir(self, d: Path) -> Optional[Skill]:
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            return None
        parsed = self._parse_skill_md(skill_md)
        if not parsed:
            return None
        files = []
        for f in sorted(d.rglob("*")):
            if (
                f.is_file()
                and ".git" not in f.parts
                and f.name not in ("SKILL.md", ".kedo_source")
            ):
                files.append(str(f.relative_to(d)))
        source = ""
        src_file = d / ".kedo_source"
        if src_file.exists():
            try:
                source = src_file.read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                pass
        return Skill(
            name=parsed["name"],
            description=parsed["description"],
            body=parsed["body"],
            dir=str(d),
            files=files,
            source=source,
            raw_yaml=parsed["raw"],
        )

    # ---------------------------------------------------------- 查询
    def list_skills(self) -> list[Skill]:
        out = []
        for d in sorted(self.skills_dir.iterdir()) if self.skills_dir.exists() else []:
            if d.is_dir():
                sk = self._load_dir(d)
                if sk:
                    out.append(sk)
        return out

    def get(self, name: str) -> Optional[Skill]:
        d = self.skills_dir / self._safe_name(name)
        if d.is_dir():
            return self._load_dir(d)
        # 兜底：frontmatter name 与目录名不一致时全扫一遍
        for sk in self.list_skills():
            if sk.name == name:
                return sk
        return None

    def catalog_for_prompt(self, max_skills: int = 20) -> str:
        """已安装 skill 的目录，注入系统 prompt（只给名字+描述，正文按需 skill_read）。"""
        skills = self.list_skills()[:max_skills]
        if not skills:
            return ""
        lines = [
            "## Available Skills",
            "下面是已安装的 skill（可复用流程指令包）。当任务匹配某个 skill 的 description 时，"
            "先用 `skill_read(name='<name>')` 读取完整指令再按它执行；按需用 file_read 读 skill 目录下的 references/scripts。",
            "",
        ]
        for sk in skills:
            lines.append(f"- **{sk.name}**: {sk.description or '(无描述)'}")
        return "\n".join(lines)

    # ---------------------------------------------------------- 安装 / 卸载
    def install(self, source: str, timeout: int = 120) -> Skill:
        """
        从 git URL 或本地路径安装一个 skill。
        - git URL（ssh://、https://、git@…）→ git clone --depth 1
        - 本地目录 → 拷贝
        解析 frontmatter 拿 name，落到 skills_dir/<name>/。只 clone/copy，不跑脚本。
        """
        source = (source or "").strip()
        if not source:
            raise ValueError("source 不能为空")
        # 防 argv flag 注入：以 - 开头会被 git/shutil 当成选项（如 --upload-pack=… → RCE）
        if source.startswith("-"):
            raise ValueError("source 不能以 '-' 开头")

        tmp = self.skills_dir / ".tmp_install"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

        is_git = bool(re.match(r"^(ssh://|https?://|git@|git://)", source)) or source.endswith(".git")
        try:
            if is_git:
                # -- 终止选项解析，user-controlled source 一律当位置参数
                env_cmd = [
                    "git", "clone", "--depth", "1", "--", source, str(tmp),
                ]
                proc = subprocess.run(
                    env_cmd,
                    capture_output=True, text=True, timeout=timeout,
                    env={
                        **_git_env(),
                    },
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"git clone 失败: {proc.stderr.strip()[:500]}")
            else:
                src_path = Path(source).expanduser()
                if not src_path.is_dir():
                    raise ValueError(f"本地路径不存在或不是目录: {src_path}")
                shutil.copytree(src_path, tmp)

            skill_md = tmp / "SKILL.md"
            if not skill_md.exists():
                raise ValueError("源中没有 SKILL.md，不是合法的 Agent Skill 包")
            parsed = self._parse_skill_md(skill_md)
            if not parsed:
                raise ValueError("SKILL.md 解析失败")
            name = self._safe_name(parsed["name"])
            dest = self.skills_dir / name
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            # 去掉 .git 以免污染 + 体积
            git_dir = tmp / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir, ignore_errors=True)
            tmp.rename(dest)
            try:
                (dest / ".kedo_source").write_text(source, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            sk = self._load_dir(dest)
            if sk is None:
                raise RuntimeError("安装后加载失败")
            logger.info(f"SkillLoader: installed skill '{sk.name}' from {source} → {dest}")
            return sk
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    def remove(self, name: str) -> bool:
        sk = self.get(name)
        if not sk:
            return False
        shutil.rmtree(sk.dir, ignore_errors=True)
        return True


def _git_env() -> dict:
    import os
    env = dict(os.environ)
    # 非交互 + 自动接受新主机 key（内网 git over ssh 常见）。不禁用 host key 校验本身。
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault(
        "GIT_SSH_COMMAND",
        "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=15",
    )
    return env
