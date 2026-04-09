"""
API 路由定义 — RESTful API + WebSocket 端点

提供任务管理、状态查询、人工控制等接口、文档浏览
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect

from api.schemas import (
    CandidateListResponse,
    CreateTaskRequest,
    CreateTaskResponse,
    DiscussionInputRequest,
    DiscussionResponse,
    PauseResumeResponse,
    ResumeCheckpointRequest,
    ResumeCheckpointResponse,
    SelectCandidateRequest,
    TaskStatus,
    TaskStatusResponse,
)
from api.websocket import ws_manager

router = APIRouter()

# 这些在 server.py 中注入
_agent_loop = None
_state_manager = None
_create_llm_client = None  # create_llm_client 函数引用，避免循环导入
_project_path: str = "."   # 项目根目录，用于文档浏览


def set_dependencies(agent_loop, state_manager, create_llm_client_fn=None, project_path: str = "."):
    global _agent_loop, _state_manager, _create_llm_client, _project_path
    _agent_loop = agent_loop
    _state_manager = state_manager
    _create_llm_client = create_llm_client_fn
    _project_path = project_path


# ============================================================
# 任务管理 API
# ============================================================

@router.post("/tasks", response_model=CreateTaskResponse)
async def create_task(req: CreateTaskRequest):
    """创建新的开发任务"""
    task_id = str(uuid.uuid4())[:8]
    # 如果请求没指定 project_path（或为默认 "."），使用 server 配置的项目路径
    project_path = req.project_path
    if project_path == ".":
        project_path = _project_path
    await _agent_loop.start_task(
        task_id=task_id,
        description=req.description,
        project_path=project_path,
    )
    return CreateTaskResponse(
        task_id=task_id,
        status=TaskStatus.PENDING,
        message=f"Task created, Agent Loop started",
    )


@router.get("/tasks", response_model=list[dict])
async def list_tasks():
    """列出所有任务"""
    return _state_manager.list_tasks()


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str):
    """获取任务详细状态"""
    status = _state_manager.get_task_status(task_id)
    if not status:
        raise HTTPException(404, f"Task {task_id} not found")
    return status


# ============================================================
# 项目管理 API
# ============================================================

@router.post("/project/clean")
async def clean_project():
    """清空项目：删除生成的文件和任务状态"""
    import shutil
    project = Path(_project_path)

    # 删除生成的 docs 目录
    docs_dir = project / "docs"
    if docs_dir.exists():
        shutil.rmtree(docs_dir)

    # 删除生成的代码文件（排除 .kedo 和 .git）
    for item in project.iterdir():
        if item.name in ('.kedo', '.git', '.gitignore', '.venv', 'node_modules'):
            continue
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)

    # 清空状态
    state_dir = project / ".kedo" / "state"
    if state_dir.exists():
        for f in state_dir.iterdir():
            if f.suffix == '.json':
                f.unlink()

    # 重置内存中的任务状态
    _state_manager._tasks.clear()
    _state_manager._checkpoints.clear()

    return {"status": "ok", "message": "Project cleaned"}


# ============================================================
# Agent 控制 API
# ============================================================

@router.post("/tasks/{task_id}/pause", response_model=PauseResumeResponse)
async def pause_task(task_id: str):
    """暂停任务执行"""
    await _state_manager.pause_task(task_id)
    return PauseResumeResponse(
        task_id=task_id,
        status=TaskStatus.PAUSED,
        message="Task paused. Agent will stop at next checkpoint.",
    )


@router.post("/tasks/{task_id}/resume", response_model=PauseResumeResponse)
async def resume_task(task_id: str):
    """恢复任务执行"""
    await _state_manager.resume_task(task_id)
    return PauseResumeResponse(
        task_id=task_id,
        status=TaskStatus.IN_PROGRESS,
        message="Task resumed.",
    )


@router.post("/tasks/{task_id}/resume-checkpoint", response_model=ResumeCheckpointResponse)
async def resume_from_checkpoint(task_id: str, req: ResumeCheckpointRequest = ResumeCheckpointRequest()):
    """从检查点恢复任务执行（支持跨会话续接）"""
    # 检查 checkpoint 是否存在
    checkpoint = await _state_manager.load_checkpoint(task_id)
    if not checkpoint:
        raise HTTPException(404, f"No checkpoint found for task {task_id}")

    try:
        await _agent_loop.resume_from_checkpoint(
            task_id=task_id,
            additional_context=req.additional_context,
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to resume: {e}")

    return ResumeCheckpointResponse(
        task_id=task_id,
        status=TaskStatus.IN_PROGRESS,
        resumed_from_step=checkpoint.current_step_index + 1,
        total_steps=len(checkpoint.plan.subtasks) if checkpoint.plan else 0,
        message=f"Resumed from step {checkpoint.current_step_index + 1}"
                + (f", context: {req.additional_context[:50]}" if req.additional_context else ""),
    )


@router.get("/tasks/resumable")
async def list_resumable_tasks():
    """列出所有可续接的历史任务（有 checkpoint 的失败/暂停任务）"""
    return _state_manager.find_resumable_tasks()


# ============================================================
# 候选版本 API
# ============================================================

@router.get("/tasks/{task_id}/candidates")
async def list_candidates(task_id: str):
    """获取任务的所有候选版本"""
    candidates = _agent_loop.versions.get_candidates(task_id)
    recommended = _agent_loop.versions.get_recommended(task_id)
    return CandidateListResponse(
        task_id=task_id,
        candidates=candidates,
        recommended_version_id=recommended.version_id if recommended else None,
    )


@router.get("/tasks/{task_id}/candidates/{version_id}")
async def get_candidate(task_id: str, version_id: str):
    """获取候选版本详情"""
    candidate = _agent_loop.versions._find(task_id, version_id)
    if not candidate:
        raise HTTPException(404, f"Candidate {version_id} not found")
    return candidate


@router.post("/tasks/{task_id}/candidates/{version_id}/select")
async def select_candidate(task_id: str, version_id: str):
    """选择候选版本进行人工测试"""
    candidate = await _agent_loop.versions.select_for_testing(task_id, version_id)
    if not candidate:
        raise HTTPException(404, f"Candidate {version_id} not found")
    return {"task_id": task_id, "version_id": version_id, "status": candidate.status.value}


# ============================================================
# 讨论 & 闭环 API
# ============================================================

@router.get("/tasks/{task_id}/discussion")
async def get_discussion(task_id: str):
    """获取当前迭代的讨论状态"""
    iteration_state = _agent_loop._iterations.get(task_id)
    if not iteration_state or not iteration_state.discussions:
        return {"task_id": task_id, "has_discussion": False, "iteration": 1}
    latest = iteration_state.discussions[-1]
    return DiscussionResponse(
        discussion_id=latest.discussion_id,
        task_id=task_id,
        iteration=latest.iteration,
        status=latest.status,
        issues=latest.issues,
        proposals=latest.proposals,
        selected_proposal_id=latest.selected_proposal_id,
    )


@router.get("/tasks/{task_id}/iterations")
async def get_iterations(task_id: str):
    """获取迭代历史"""
    iteration_state = _agent_loop._iterations.get(task_id)
    if not iteration_state:
        return {"task_id": task_id, "current_iteration": 1, "max_iterations": 5, "discussions": []}
    return {
        "task_id": task_id,
        "current_iteration": iteration_state.current_iteration,
        "max_iterations": iteration_state.max_iterations,
        "is_forced_pause": iteration_state.is_forced_pause,
        "discussions": [
            {
                "discussion_id": d.discussion_id,
                "iteration": d.iteration,
                "trigger": d.trigger,
                "status": d.status.value,
                "issue_count": len(d.issues),
                "proposal_count": len(d.proposals),
                "selected_proposal_id": d.selected_proposal_id,
                "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
            }
            for d in iteration_state.discussions
        ],
    }


@router.post("/tasks/{task_id}/discussion/input")
async def submit_discussion_input(task_id: str, req: DiscussionInputRequest):
    """人工参与讨论：选择方案或追加意见"""
    await _agent_loop.submit_discussion_input(
        task_id=task_id,
        action=req.action,
        proposal_id=req.proposal_id,
        human_input=req.human_input,
        additional_constraints=req.additional_constraints,
    )
    return {
        "task_id": task_id,
        "proposal_id": req.proposal_id,
        "message": "Discussion input submitted",
    }


# ============================================================
# LLM 提供商切换 API
# ============================================================

@router.get("/llm/status")
async def get_llm_status():
    """获取当前 LLM 提供商信息"""
    llm = _agent_loop.planner._llm if _agent_loop else None
    provider = "unknown"
    model = "unknown"
    if llm:
        cls_name = type(llm).__name__
        provider_map = {
            "AnthropicClient": "claude",
            "KimiClient": "kimi",
            "OpenAIClient": "openai",
            "OllamaClient": "ollama",
            "MockLLMClient": "mock",
        }
        provider = provider_map.get(cls_name, cls_name)
        # 区分 Kimi Code 和 Kimi 通用
        if cls_name == "KimiClient" and hasattr(llm, "base_url"):
            if "api.kimi.com" in llm.base_url:
                provider = "kimi-code"
        model = getattr(llm, "model", "unknown")
    return {"provider": provider, "model": model}


@router.post("/llm/switch")
async def switch_llm(req: dict = Body(...)):
    """
    运行时切换 LLM 提供商

    Body: {"provider": "claude"|"kimi"|"mock", "api_key": "...", "model": "..."}
    """
    if not _create_llm_client:
        return {"success": False, "error": "LLM 工厂未初始化"}
    provider = req.get("provider", "")
    api_key = req.get("api_key", "")
    model = req.get("model", "")

    # 构建临时 config
    switch_config = {"llm_provider": provider}

    if provider == "claude":
        switch_config["llm_provider"] = "anthropic"
        if api_key:
            switch_config["anthropic_api_key"] = api_key
        if model:
            switch_config["model"] = model
        else:
            switch_config["model"] = "claude-sonnet-4-20250514"
    elif provider == "kimi-code":
        # Kimi Code 2.5 — 编程专用端点
        switch_config["llm_provider"] = "kimi"
        if api_key:
            switch_config["kimi_api_key"] = api_key
        switch_config["kimi_base_url"] = "https://api.kimi.com/coding/v1"
        if model:
            switch_config["model"] = model
        else:
            switch_config["model"] = "kimi-k2.5"
    elif provider == "kimi":
        # Kimi K2.5 — 通用 Moonshot 端点
        switch_config["llm_provider"] = "kimi"
        if api_key:
            switch_config["kimi_api_key"] = api_key
        switch_config["kimi_base_url"] = "https://api.moonshot.ai/v1"
        if model:
            switch_config["model"] = model
        else:
            switch_config["model"] = "kimi-k2.5"
    elif provider == "mock":
        switch_config["llm_provider"] = "mock"
    else:
        return {"success": False, "error": f"不支持的提供商: {provider}"}

    # 环境变量也注入（让 create_llm_client 可以读取）
    import os
    if provider == "claude" and api_key:
        os.environ["ANTHROPIC_API_KEY"] = api_key
    elif provider in ("kimi", "kimi-code") and api_key:
        os.environ["KIMI_API_KEY"] = api_key

    try:
        new_client = _create_llm_client(switch_config)
        # 热替换: planner 和 evaluator 内部的 _llm
        _agent_loop.planner._llm = new_client
        _agent_loop.evaluator._llm = new_client
        # 更新工具中的 code_generator 的 _llm / llm_client
        if hasattr(_agent_loop, 'tool_registry'):
            for tool in _agent_loop.tool_registry._tools.values():
                if hasattr(tool, '_llm'):
                    tool._llm = new_client
                elif hasattr(tool, 'llm_client'):
                    tool.llm_client = new_client

        actual_provider = provider
        actual_model = getattr(new_client, "model", "mock")
        return {
            "success": True,
            "provider": actual_provider,
            "model": actual_model,
            "message": f"已切换到 {actual_provider} ({actual_model})",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/tasks/{task_id}/logs")
async def get_logs(task_id: str, limit: int = 100):
    """获取任务日志"""
    status = _state_manager.get_task_status(task_id)
    if not status:
        raise HTTPException(404, f"Task {task_id} not found")
    return {"task_id": task_id, "logs": status.logs[-limit:]}


@router.get("/tasks/{task_id}/events")
async def get_events(task_id: str, limit: int = 50):
    """获取任务事件历史"""
    events = _state_manager.event_bus.get_history(task_id, limit)
    return {
        "task_id": task_id,
        "events": [
            {
                "type": e.event_type.value,
                "timestamp": e.timestamp.isoformat(),
                "data": e.data,
            }
            for e in events
        ],
    }


# ============================================================
# 文档浏览 API (docs/ 目录 + 需求文档)
# ============================================================

def _get_docs_dir() -> Path:
    """获取 docs 目录路径"""
    return Path(_project_path) / "docs"


def _safe_resolve(base: Path, rel_path: str) -> Path:
    """安全地解析路径，防止路径遍历攻击"""
    resolved = (base / rel_path).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        raise HTTPException(403, "Access denied: path traversal detected")
    return resolved


@router.get("/docs")
async def list_docs():
    """
    列出 docs/ 目录下的文件树

    返回递归的文件/目录结构，支持 Markdown、Mermaid 等文件
    """
    docs_dir = _get_docs_dir()
    if not docs_dir.is_dir():
        return {"tree": [], "docs_path": str(docs_dir), "exists": False}

    def _build_tree(directory: Path, rel_prefix: str = "") -> list:
        items = []
        try:
            entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return items
        for entry in entries:
            # 跳过隐藏文件和常见无用目录
            if entry.name.startswith(".") or entry.name in ("__pycache__", "node_modules"):
                continue
            rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
            if entry.is_dir():
                children = _build_tree(entry, rel)
                items.append({
                    "name": entry.name,
                    "path": rel,
                    "type": "directory",
                    "children": children,
                })
            else:
                items.append({
                    "name": entry.name,
                    "path": rel,
                    "type": "file",
                    "size": entry.stat().st_size,
                    "ext": entry.suffix.lower(),
                })
        return items

    tree = _build_tree(docs_dir)
    return {"tree": tree, "docs_path": str(docs_dir), "exists": True}


@router.get("/docs/file")
async def read_doc_file(path: str):
    """
    读取 docs/ 目录下的文件内容

    Query: ?path=architecture.md 或 ?path=api/api-design.md
    """
    docs_dir = _get_docs_dir()
    if not docs_dir.is_dir():
        raise HTTPException(404, "docs/ directory not found")

    file_path = _safe_resolve(docs_dir, path)
    if not file_path.is_file():
        raise HTTPException(404, f"File not found: {path}")

    # 只允许读取文本类文件
    text_extensions = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".html", ".css", ".js",
                       ".py", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cpp", ".h",
                       ".sh", ".bash", ".sql", ".xml", ".csv", ".ini", ".cfg", ".conf", ".env",
                       ".mermaid", ".plantuml", ".puml", ".rst", ".adoc", ".log", ""}
    ext = file_path.suffix.lower()
    if ext not in text_extensions:
        raise HTTPException(415, f"Unsupported file type: {ext}")

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = file_path.read_text(encoding="gbk")
        except Exception:
            raise HTTPException(415, "Cannot read file: unsupported encoding")

    return {
        "path": path,
        "name": file_path.name,
        "content": content,
        "size": file_path.stat().st_size,
        "ext": ext,
    }


@router.put("/docs/file")
async def write_doc_file(body: dict = Body(...)):
    """
    写入 / 更新 docs/ 目录下的文件

    Body: {"path": "architecture.md", "content": "..."}
    如果文件不存在则自动创建（含子目录）
    """
    rel_path = body.get("path", "")
    content = body.get("content", "")
    if not rel_path:
        raise HTTPException(400, "path is required")

    docs_dir = _get_docs_dir()
    docs_dir.mkdir(parents=True, exist_ok=True)
    file_path = _safe_resolve(docs_dir, rel_path)

    # 确保父目录存在
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    project = Path(_project_path)
    return {
        "path": rel_path,
        "message": f"文件已保存: {rel_path}",
        "size": len(content),
    }


@router.get("/requirement")
async def get_requirement():
    """
    获取统一需求文档

    优先级: docs/requirement.md > .kedo/requirement.md
    """
    project = Path(_project_path)
    candidates = [
        project / "docs" / "requirement.md",
        project / ".kedo" / "requirement.md",
    ]
    for fp in candidates:
        if fp.is_file():
            try:
                content = fp.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = fp.read_text(encoding="gbk")
            return {
                "content": content,
                "path": str(fp.relative_to(project)),
                "exists": True,
            }
    return {"content": "", "path": "docs/requirement.md", "exists": False}


@router.put("/requirement")
async def update_requirement(body: dict = Body(...)):
    """
    更新统一需求文档

    Body: {"content": "..."}
    默认保存到 docs/requirement.md
    """
    content = body.get("content", "")
    project = Path(_project_path)
    docs_dir = project / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    fp = docs_dir / "requirement.md"
    fp.write_text(content, encoding="utf-8")
    return {"path": str(fp.relative_to(project)), "message": "需求文档已保存", "size": len(content)}


# ============================================================
# 代码 & 打包 & 部署 & 测试监控 API
# ============================================================

@router.get("/code/status")
async def get_code_status():
    """获取代码生成监控状态汇总"""
    tasks = _state_manager.list_tasks()
    records = []
    total = success = failed = running = 0
    for t in tasks:
        task_id = t.get("task_id", t.get("id", ""))
        status = _state_manager.get_task_status(task_id)
        if not status:
            continue

        # 从 checkpoint 中获取 code_changes
        code_changes = getattr(status, "code_changes", []) or []
        if not code_changes:
            # 也检查 logs 判断是否有代码相关步骤
            logs = getattr(status, "logs", []) or []
            has_code = False
            for log in logs:
                log_text = log if isinstance(log, str) else str(log)
                if "code" in log_text.lower() or "coding" in log_text.lower() or "代码" in log_text:
                    has_code = True
                    break
            if not has_code and getattr(status, "current_step", "") not in ("coding", "代码生成"):
                continue

        total += 1
        task_status_val = getattr(status, "status", None)
        if task_status_val:
            s = task_status_val.value if hasattr(task_status_val, "value") else str(task_status_val)
        else:
            s = "pending"
        if s in ("completed", "success"):
            success += 1
            badge = "success"
        elif s in ("failed", "error"):
            failed += 1
            badge = "failed"
        elif s in ("in_progress", "running"):
            running += 1
            badge = "running"
        else:
            badge = "pending"

        # 统计文件数和行数
        file_count = len(code_changes)
        line_count = sum(
            len((c.content or "").splitlines()) for c in code_changes
        )

        # 构建生成文件列表
        generated_files = []
        for c in code_changes:
            generated_files.append({
                "path": c.file_path,
                "action": c.action,
                "lines": len((c.content or "").splitlines()),
            })

        records.append({
            "id": task_id,
            "task": t.get("description", task_id),
            "files": file_count,
            "lines": line_count,
            "status": badge,
            "start_time": t.get("created_at", "-"),
            "duration": "-",
            "generated_files": generated_files,
        })

    # ★ 回退：如果 checkpoint 中没有文件记录，扫描项目磁盘上的源码文件
    project_files = _scan_project_source_files()
    return {
        "total": total, "success": success, "failed": failed, "running": running,
        "records": records,
        "project_files": project_files,
        "project_path": str(Path(_project_path).resolve()),
    }


def _scan_project_source_files() -> list[dict]:
    """扫描项目目录中的源码文件（排除 docs/.kedo/.git 等）"""
    project = Path(_project_path)
    if not project.is_dir():
        return []

    source_extensions = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
        ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".bash",
        ".html", ".css", ".scss", ".less", ".vue", ".svelte",
        ".json", ".yaml", ".yml", ".toml", ".xml", ".sql",
        ".dockerfile", ".makefile", ".cmake",
        ".md", ".txt", ".cfg", ".ini", ".conf", ".env",
    }
    skip_dirs = {".kedo", ".git", ".venv", "node_modules", "__pycache__", ".cache", "build", "dist"}

    files = []
    for item in project.rglob("*"):
        if item.is_dir():
            continue
        # 跳过排除目录下的文件
        parts = item.relative_to(project).parts
        if any(p in skip_dirs for p in parts):
            continue
        # 匹配源码扩展名（或无扩展名但名字匹配如 Makefile, Dockerfile）
        ext = item.suffix.lower()
        name_lower = item.name.lower()
        if ext in source_extensions or name_lower in ("makefile", "dockerfile", "cmakelists.txt", "rakefile", "gemfile"):
            try:
                stat = item.stat()
                files.append({
                    "path": str(item.relative_to(project)),
                    "abs_path": str(item),
                    "name": item.name,
                    "ext": ext,
                    "size": stat.st_size,
                    "lines": _count_lines(item),
                })
            except (OSError, PermissionError):
                continue

    # 按路径排序
    files.sort(key=lambda f: f["path"])
    return files


def _count_lines(file_path: Path) -> int:
    """快速统计文件行数"""
    try:
        return sum(1 for _ in file_path.open("rb"))
    except Exception:
        return 0


@router.get("/code/file")
async def get_code_file(task_id: str = "", file_path: str = ""):
    """获取指定任务中生成的代码文件内容（支持 checkpoint 和磁盘读取）"""
    # 先尝试从 checkpoint 读取
    if task_id:
        status = _state_manager.get_task_status(task_id)
        if status:
            code_changes = getattr(status, "code_changes", []) or []
            for c in code_changes:
                if c.file_path == file_path:
                    return {
                        "file_path": c.file_path,
                        "action": c.action,
                        "content": c.content or "",
                        "diff": c.diff or "",
                        "lines": len((c.content or "").splitlines()),
                    }

    # 从磁盘读取
    from pathlib import Path as _Path
    fp = _Path(file_path)
    if fp.is_file():
        try:
            content = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = fp.read_text(encoding="gbk")
            except Exception:
                raise HTTPException(415, "Cannot read file: unsupported encoding")
        return {
            "file_path": file_path,
            "action": "disk",
            "content": content,
            "diff": "",
            "lines": len(content.splitlines()),
        }

    raise HTTPException(404, f"File not found: {file_path}")


@router.get("/build/status")
async def get_build_status():
    """获取打包监控状态汇总"""
    tasks = _state_manager.list_tasks()
    records = []
    total = success = failed = running = 0
    for t in tasks:
        status = _state_manager.get_task_status(t.get("task_id", t.get("id", "")))
        if not status:
            continue
        logs = getattr(status, "logs", []) or []
        has_build = False
        for log in logs:
            log_text = log if isinstance(log, str) else str(log)
            if "build" in log_text.lower() or "pack" in log_text.lower() or "打包" in log_text:
                has_build = True
                break
        if has_build or getattr(status, "current_step", "") in ("build", "打包"):
            total += 1
            task_status_val = getattr(status, "status", None)
            if task_status_val:
                s = task_status_val.value if hasattr(task_status_val, "value") else str(task_status_val)
            else:
                s = "pending"
            if s in ("completed", "success"):
                success += 1
                badge = "success"
            elif s in ("failed", "error"):
                failed += 1
                badge = "failed"
            elif s in ("in_progress", "running"):
                running += 1
                badge = "running"
            else:
                badge = "pending"
            records.append({
                "id": t.get("task_id", t.get("id", "")),
                "task": t.get("description", t.get("task_id", "")),
                "artifact": "-",
                "size": "-",
                "status": badge,
                "start_time": t.get("created_at", "-"),
                "duration": "-",
            })
    return {"total": total, "success": success, "failed": failed, "running": running, "records": records}

@router.get("/deploy/status")
async def get_deploy_status():
    """获取部署监控状态汇总，含部署地址列表和 Agent 列表"""
    tasks = _state_manager.list_tasks()
    records = []
    total = success = failed = running = 0
    for t in tasks:
        status = _state_manager.get_task_status(t.get("task_id", t.get("id", "")))
        if not status:
            continue
        deploy_step = None
        logs = getattr(status, "logs", []) or []
        for log in logs:
            log_text = log if isinstance(log, str) else str(log)
            if "deploy" in log_text.lower() or "部署" in log_text:
                deploy_step = log_text
                break
        if deploy_step or getattr(status, "current_step", "") in ("deploy", "部署"):
            total += 1
            task_status_val = getattr(status, "status", None)
            if task_status_val:
                s = task_status_val.value if hasattr(task_status_val, "value") else str(task_status_val)
            else:
                s = "pending"
            if s in ("completed", "success"):
                success += 1
                badge = "success"
            elif s in ("failed", "error"):
                failed += 1
                badge = "failed"
            elif s in ("in_progress", "running"):
                running += 1
                badge = "running"
            else:
                badge = "pending"
            records.append({
                "id": t.get("task_id", t.get("id", "")),
                "task": t.get("description", t.get("task_id", "")),
                "env": "dev",
                "url": getattr(status, "deploy_url", "") or "-",
                "status": badge,
                "start_time": t.get("created_at", "-"),
                "duration": "-",
            })

    # 部署地址与状态列表
    endpoints = _collect_deploy_endpoints()

    # Agent 列表
    agents = _collect_agent_list()

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "running": running,
        "records": records,
        "endpoints": endpoints,
        "agents": agents,
    }


def _collect_deploy_endpoints() -> list:
    """收集当前已知的部署地址信息"""
    endpoints = []
    # 从 Kedo 自身 API 服务收集
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "127.0.0.1"

    endpoints.append({
        "name": "Kedo Dashboard",
        "env": "local",
        "url": f"http://{local_ip}:8080",
        "port": "8080",
        "status": "success",
        "last_deploy": "-",
    })
    endpoints.append({
        "name": "Kedo API",
        "env": "local",
        "url": f"http://{local_ip}:8080/api",
        "port": "8080",
        "status": "success",
        "last_deploy": "-",
    })

    # 从任务日志中提取部署过的地址
    if _state_manager:
        for t in _state_manager.list_tasks():
            status = _state_manager.get_task_status(t.get("task_id", t.get("id", "")))
            if not status:
                continue
            deploy_url = getattr(status, "deploy_url", None)
            if deploy_url:
                endpoints.append({
                    "name": t.get("description", "Service"),
                    "env": "dev",
                    "url": deploy_url,
                    "port": "-",
                    "status": "success",
                    "last_deploy": t.get("created_at", "-"),
                })
    return endpoints


def _collect_agent_list() -> list:
    """收集当前可用的 Agent 信息"""
    agents = []

    # Planner agent
    if _agent_loop and hasattr(_agent_loop, "planner"):
        planner = _agent_loop.planner
        llm_name = type(getattr(planner, "_llm", None)).__name__ if hasattr(planner, "_llm") else "unknown"
        agents.append({
            "name": "Planner",
            "type": llm_name,
            "stage": "需求分析 / SDD生成",
            "status": "success",
            "task_count": len(_state_manager.list_tasks()) if _state_manager else 0,
            "last_activity": "-",
        })

    # Evaluator agent
    if _agent_loop and hasattr(_agent_loop, "evaluator"):
        evaluator = _agent_loop.evaluator
        llm_name = type(getattr(evaluator, "_llm", None)).__name__ if hasattr(evaluator, "_llm") else "unknown"
        agents.append({
            "name": "Evaluator",
            "type": llm_name,
            "stage": "测试评估 / 质量检查",
            "status": "success",
            "task_count": 0,
            "last_activity": "-",
        })

    # Tool agents
    if _agent_loop and hasattr(_agent_loop, "tool_registry"):
        registry = _agent_loop.tool_registry
        tools_dict = getattr(registry, "_tools", {})
        tool_stage_map = {
            "CodeGeneratorTool": ("代码生成", "coding"),
            "TestRunnerTool": ("测试执行", "testing"),
            "ShellExecutorTool": ("命令执行", "deploy / build"),
            "GitTool": ("版本管理", "coding / deploy"),
            "FileReadTool": ("文件读取", "全流程"),
            "FileWriteTool": ("文件写入", "全流程"),
            "FileSearchTool": ("文件搜索", "全流程"),
        }
        for tool_name, tool_obj in tools_dict.items():
            cls_name = type(tool_obj).__name__
            stage_info = tool_stage_map.get(cls_name, (tool_name, "-"))
            agents.append({
                "name": tool_name,
                "type": cls_name,
                "stage": stage_info[1] if len(stage_info) > 1 else "-",
                "status": "success",
                "task_count": 0,
                "last_activity": "-",
            })
    return agents


@router.get("/test/status")
async def get_test_status():
    """获取测试监控状态汇总"""
    tasks = _state_manager.list_tasks()
    suites = []
    total = passed = failed_count = skipped = 0
    for t in tasks:
        status = _state_manager.get_task_status(t.get("task_id", t.get("id", "")))
        if not status:
            continue
        logs = getattr(status, "logs", []) or []
        has_test = False
        for log in logs:
            log_text = log if isinstance(log, str) else str(log)
            if "test" in log_text.lower() or "测试" in log_text:
                has_test = True
                break
        if has_test or getattr(status, "current_step", "") in ("testing", "测试"):
            task_status_val = getattr(status, "status", None)
            if task_status_val:
                s = task_status_val.value if hasattr(task_status_val, "value") else str(task_status_val)
            else:
                s = "pending"
            suite_total = 1
            suite_passed = 1 if s in ("completed", "success") else 0
            suite_failed = 1 if s in ("failed", "error") else 0
            total += suite_total
            passed += suite_passed
            failed_count += suite_failed
            if s in ("completed", "success"):
                badge = "success"
            elif s in ("failed", "error"):
                badge = "failed"
            elif s in ("in_progress", "running"):
                badge = "running"
            else:
                badge = "pending"
            suites.append({
                "name": t.get("description", t.get("task_id", "")),
                "total": suite_total,
                "passed": suite_passed,
                "failed": suite_failed,
                "status": badge,
                "duration": "-",
            })
    # 测试地址列表
    endpoints = _collect_test_endpoints()

    # Agent 列表（与部署共用采集，筛选测试相关）
    agents = _collect_agent_list()

    return {
        "total": total,
        "passed": passed,
        "failed": failed_count,
        "skipped": skipped,
        "suites": suites,
        "endpoints": endpoints,
        "agents": agents,
    }


def _collect_test_endpoints() -> list:
    """收集测试相关的地址信息"""
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "127.0.0.1"

    endpoints = []
    endpoints.append({
        "name": "Kedo API (测试入口)",
        "env": "local",
        "url": f"http://{local_ip}:8080/api",
        "port": "8080",
        "status": "success",
    })

    # 从任务日志中提取测试环境地址
    if _state_manager:
        for t in _state_manager.list_tasks():
            status = _state_manager.get_task_status(t.get("task_id", t.get("id", "")))
            if not status:
                continue
            test_url = getattr(status, "test_url", None)
            if test_url:
                endpoints.append({
                    "name": t.get("description", "Test Service"),
                    "env": "test",
                    "url": test_url,
                    "port": "-",
                    "status": "success",
                })
    return endpoints


# ============================================================
# WebSocket 端点
# ============================================================

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, task_id: Optional[str] = None):
    """
    WebSocket 实时推送端点

    连接后自动接收所有事件，可选指定 task_id 只接收特定任务的事件
    """
    await ws_manager.connect(websocket, task_id)
    try:
        while True:
            # 接收客户端消息 (用于 ping/pong 或控制命令)
            data = await websocket.receive_json()

            # 处理客户端命令
            action = data.get("action")
            target_task = data.get("task_id", task_id)

            if action == "pause" and target_task:
                await _state_manager.pause_task(target_task)
            elif action == "resume" and target_task:
                await _state_manager.resume_task(target_task)
            elif action == "subscribe" and target_task:
                if target_task not in ws_manager._task_subscriptions:
                    ws_manager._task_subscriptions[target_task] = []
                ws_manager._task_subscriptions[target_task].append(websocket)

    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
