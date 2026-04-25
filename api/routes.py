"""
API 路由定义 — RESTful API + WebSocket 端点

提供任务管理、状态查询、人工控制等接口、文档浏览
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
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
_agent_loop = None  # M3 退役后保持 None；下面 routes 都用 if _agent_loop 守护
_react_agent = None
_state_manager = None
_version_manager = None  # candidate API 直接用，不再走 _agent_loop.versions
_planner = None          # planner._llm 兼容路径用
_evaluator = None        # health 检查内省 + LLM 热切换用
_reviewer = None         # 方案 C 独立审查 Agent，/llm/status 需要展示是否激活
_create_llm_client = None  # create_llm_client 函数引用，避免循环导入
_project_path: str = "."   # 项目根目录，用于文档浏览


def set_dependencies(
    agent_loop,
    state_manager,
    create_llm_client_fn=None,
    project_path: str = ".",
    react_agent=None,
    version_manager=None,
    planner=None,
    evaluator=None,
    reviewer=None,
):
    global _agent_loop, _react_agent, _state_manager, _version_manager
    global _planner, _evaluator, _reviewer, _create_llm_client, _project_path
    _agent_loop = agent_loop
    _react_agent = react_agent
    _state_manager = state_manager
    _version_manager = version_manager
    _planner = planner
    _evaluator = evaluator
    _reviewer = reviewer
    _create_llm_client = create_llm_client_fn
    _project_path = project_path


# ============================================================
# 任务管理 API
# ============================================================


@router.post("/tasks", response_model=CreateTaskResponse)
async def create_task(req: CreateTaskRequest):
    """创建新的开发任务（使用 ReactAgent）"""
    task_id = str(uuid.uuid4())[:8]
    project_path = req.project_path
    if project_path == ".":
        project_path = _project_path

    # 优先使用 ReactAgent，回退到旧 AgentLoop
    agent = _react_agent or _agent_loop
    await agent.start_task(
        task_id=task_id,
        description=req.description,
        project_path=project_path,
    )
    return CreateTaskResponse(
        task_id=task_id,
        status=TaskStatus.PENDING,
        message=f"Task created, ReactAgent started",
    )


@router.get("/tasks", response_model=list[dict])
async def list_tasks():
    """列出所有任务"""
    return _state_manager.list_tasks()


@router.get("/tasks/resumable")
async def list_resumable_tasks():
    """列出所有可续接的历史任务（有 checkpoint 的非完成状态任务）"""
    return _state_manager.find_resumable_tasks()


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str):
    """获取任务详细状态"""
    status = _state_manager.get_task_status(task_id)
    if not status:
        raise HTTPException(404, f"Task {task_id} not found")
    return status


# ============================================================
# 产品需求智能总结
# ============================================================

@router.get("/product-summary")
async def product_summary():
    """
    智能总结所有任务对话，生成当前产品最新需求提示词。

    收集所有任务的描述 + 状态 + 关键结果，调用 LLM 生成一份
    结构化的产品需求文档。前端在讨论面板展示。
    """
    tasks = _state_manager.list_tasks()
    if not tasks:
        return {"summary": "暂无任务，请先创建任务。", "task_count": 0}

    # 构建任务上下文
    task_lines = []
    for t in tasks:
        status = t.get("status", "unknown")
        desc = t.get("description", "")[:500]
        task_id = t.get("task_id", t.get("id", "?"))
        # 尝试获取详细状态（含 plan subtasks）
        detail = _state_manager.get_task_status(task_id)
        subtask_info = ""
        if detail and hasattr(detail, "plan") and detail.plan:
            subs = detail.plan.subtasks if hasattr(detail.plan, "subtasks") else []
            completed = sum(1 for s in subs if getattr(s, "status", "") == "completed")
            subtask_info = f" ({completed}/{len(subs)} 步骤完成)"
        task_lines.append(f"- [{status}{subtask_info}] {desc}")

    tasks_context = "\n".join(task_lines)

    # 调用 LLM 生成需求总结
    llm = (_react_agent.llm if _react_agent else None) \
        or (_planner._llm if _planner else None) \
        or (_agent_loop.planner._llm if _agent_loop else None)
    if llm is None:
        return {"summary": "LLM 未配置，无法生成需求总结。", "task_count": len(tasks)}
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个产品需求分析师。根据用户与 AI 开发工具的历史对话（每条对话是一个开发任务），"
                "生成一份**当前产品的最新需求提示词**。\n\n"
                "要求：\n"
                "1. 提炼核心功能需求（已实现的标 ✅，进行中的标 🔧，失败的标 ❌）\n"
                "2. 总结产品当前状态（一句话）\n"
                "3. 生成一份可以直接用于下次开发的**需求提示词**（如果要从零开始重新描述这个产品应该怎么写）\n"
                "4. 列出可能的下一步改进方向\n\n"
                "输出格式用 Markdown。语言：中文。"
            ),
        },
        {
            "role": "user",
            "content": f"以下是该项目的所有历史任务对话：\n\n{tasks_context}\n\n请生成产品需求总结。",
        },
    ]

    try:
        summary = await llm.chat(messages)
    except Exception as e:
        summary = f"LLM 总结生成失败: {e}"

    return {
        "summary": summary,
        "task_count": len(tasks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


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
        # M2: 优先走 ReactAgent.resume_from_checkpoint（基于 messages 历史还原）
        # 旧 AgentLoop 的 smart_continuation 路径作为兼容保留：仅当 checkpoint 没有
        # messages（pre-M2 版本写入的）才回退
        if _react_agent and checkpoint.messages:
            await _react_agent.resume_from_checkpoint(
                task_id=task_id,
                additional_context=req.additional_context,
            )
        elif _agent_loop:
            await _agent_loop.resume_from_checkpoint(
                task_id=task_id,
                additional_context=req.additional_context,
            )
        else:
            raise HTTPException(
                500,
                "Cannot resume: ReactAgent unavailable and AgentLoop deprecated. "
                "Pre-M2 checkpoints (without messages history) cannot be resumed."
            )
    except HTTPException:
        raise
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



# ============================================================
# 候选版本 API
# ============================================================

@router.get("/tasks/{task_id}/candidates")
async def list_candidates(task_id: str):
    """获取任务的所有候选版本"""
    vm = _version_manager or (_agent_loop.versions if _agent_loop else None)
    if not vm:
        return CandidateListResponse(task_id=task_id, candidates=[], recommended_version_id=None)
    candidates = vm.get_candidates(task_id)
    recommended = vm.get_recommended(task_id)
    return CandidateListResponse(
        task_id=task_id,
        candidates=candidates,
        recommended_version_id=recommended.version_id if recommended else None,
    )


@router.get("/tasks/{task_id}/candidates/{version_id}")
async def get_candidate(task_id: str, version_id: str):
    """获取候选版本详情"""
    vm = _version_manager or (_agent_loop.versions if _agent_loop else None)
    if not vm:
        raise HTTPException(404, "VersionManager unavailable")
    candidate = vm._find(task_id, version_id)
    if not candidate:
        raise HTTPException(404, f"Candidate {version_id} not found")
    return candidate


@router.post("/tasks/{task_id}/candidates/{version_id}/select")
async def select_candidate(task_id: str, version_id: str):
    """选择候选版本进行人工测试"""
    vm = _version_manager or (_agent_loop.versions if _agent_loop else None)
    if not vm:
        raise HTTPException(404, "VersionManager unavailable")
    candidate = await vm.select_for_testing(task_id, version_id)
    if not candidate:
        raise HTTPException(404, f"Candidate {version_id} not found")
    return {"task_id": task_id, "version_id": version_id, "status": candidate.status.value}


# ============================================================
# 讨论 & 闭环 API
# ============================================================

@router.get("/tasks/{task_id}/discussion")
async def get_discussion(task_id: str):
    """获取当前活跃讨论。
    优先读 ReactAgent propose_alternatives 工具写入的 state.get_discussion；
    fallback 到旧 AgentLoop iteration_state（M3 退役后永远为 None）。"""
    # ★ 新路径：ReactAgent propose_alternatives 的活跃 proposals
    if _state_manager:
        active = _state_manager.get_discussion(task_id)
        if active:
            # 转成前端期望的 DiscussionResponse 类似结构
            return {
                "task_id": task_id,
                "has_discussion": True,
                "iteration": 1,
                "issues": [],
                "proposals": active["proposals"],
                "summary": active.get("summary", ""),
                "started_at": active.get("started_at", ""),
                "source": "propose_alternatives",
            }
    # Fallback：旧 AgentLoop discussion（M3 后 _agent_loop=None，此分支永远跳过）
    iteration_state = _agent_loop._iterations.get(task_id) if _agent_loop else None
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
    iteration_state = _agent_loop._iterations.get(task_id) if _agent_loop else None
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
    """人工参与讨论：选择方案或追加意见。同时投递到 ReactAgent propose_alternatives queue
    和旧 AgentLoop 的 discussion queue（M3 退役 AgentLoop 后只走前者）。"""
    payload = {
        "action": req.action,
        "proposal_id": req.proposal_id,
        "human_input": req.human_input,
        "additional_constraints": req.additional_constraints or [],
    }
    # 新路径：ReactAgent propose_alternatives 工具
    try:
        from tools.propose_alternatives_tool import get_discussion_queue
        await get_discussion_queue(task_id).put(payload)
    except Exception as e:
        # 不阻塞旧路径
        import logging as _l
        _l.getLogger(__name__).debug(f"propose_alternatives queue put failed: {e}")
    # 兼容路径：旧 AgentLoop（M3 后可删）
    if _agent_loop:
        try:
            await _agent_loop.submit_discussion_input(
                task_id=task_id,
                action=req.action,
                proposal_id=req.proposal_id,
                human_input=req.human_input,
                additional_constraints=req.additional_constraints,
            )
        except Exception:
            pass
    return {
        "task_id": task_id,
        "proposal_id": req.proposal_id,
        "message": "Discussion input submitted",
    }


# ============================================================
# 轻量闲聊 API（不创建 task）
# ============================================================


@router.post("/chat")
async def quick_chat(req: dict = Body(...)):
    """
    轻量问答：直接调 LLM 返回回答，不创建 task、不走规划流水线。
    Body: {"message": "你好"}
    """
    message = req.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")

    llm = (_react_agent.llm if _react_agent else None) \
        or (_planner._llm if _planner else None) \
        or (_agent_loop.planner._llm if _agent_loop else None)
    if not llm:
        raise HTTPException(status_code=503, detail="LLM 未初始化")

    model_name = getattr(llm, "model", "unknown")
    system_prompt = (
        "你是 kedo —— 一个由大语言模型驱动的自动化开发助手。"
        "你不是 Qwen、ChatGPT 或其他模型的人格，你就是 kedo。"
        "用户此刻是在与你闲聊或询问元信息（身份、能力、模型等），不是在提开发需求。"
        "请直接、简洁地中文回答，不要生成计划或代码。"
        f"当前底层模型: {model_name}。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    try:
        answer = await llm.chat(messages)
    except Exception as e:
        answer = f"（LLM 调用失败: {e}）"

    return {"answer": answer, "model": model_name}


# ============================================================
# LLM 提供商切换 API
# ============================================================

_PROVIDER_MAP = {
    "AnthropicClient": "claude",
    "KimiClient": "kimi",
    "OpenAIClient": "openai",
    "DeepseekClient": "deepseek",
    "OllamaClient": "ollama",
    "MockLLMClient": "mock",
}


def _identify_llm(llm) -> tuple[str, str]:
    """把 LLM client 实例识别成 (provider, model)，供 /llm/status 与 reviewer 展示复用。"""
    if not llm:
        return "unknown", "unknown"
    cls_name = type(llm).__name__
    provider = _PROVIDER_MAP.get(cls_name, cls_name)
    # 区分 Kimi Code 和 Kimi 通用
    if cls_name == "KimiClient" and getattr(llm, "base_url", ""):
        if "api.kimi.com" in llm.base_url:
            provider = "kimi-code"
    return provider, getattr(llm, "model", "unknown")


@router.get("/llm/status")
async def get_llm_status():
    """获取当前 LLM 提供商信息（含方案 C 双 Agent 的 Reviewer 状态）"""
    llm = (_react_agent.llm if _react_agent else None) \
        or (_planner._llm if _planner else None) \
        or (_agent_loop.planner._llm if _agent_loop else None)
    provider, model = _identify_llm(llm)

    # 方案 C：独立审查 Agent 是否激活
    reviewer_info: dict = {"active": False}
    if _reviewer is not None and _reviewer.is_active:
        # Reviewer 内部持有独立 LLM client，复用同一套识别逻辑
        rev_llm = getattr(_reviewer, "_llm", None)
        rev_provider, rev_model = _identify_llm(rev_llm)
        reviewer_info = {
            "active": True,
            "provider": rev_provider,
            "model": rev_model,
            "min_score": _reviewer.min_score,
        }

    return {
        "provider": provider,
        "model": model,
        "project_path": _project_path,
        "reviewer": reviewer_info,
    }


def _persist_llm_config(switch_config: dict) -> tuple[bool, str]:
    """
    把 LLM 配置合并写入 ~/.config/kedo/config.yaml，下次启动自动恢复。
    只持久化 LLM 相关字段，其他字段保留不动。文件权限 0600（含 api key）。
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return False, "PyYAML 未安装"
    cfg_path = Path.home() / ".config" / "kedo" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        except Exception as e:
            return False, f"读取已有配置失败: {e}"

    # 只合并 LLM 相关字段
    keys = (
        "llm_provider", "model",
        "anthropic_api_key", "kimi_api_key", "kimi_base_url", "openai_api_key",
        "deepseek_api_key", "deepseek_base_url",
    )
    changed = False
    for k in keys:
        if k in switch_config and switch_config[k]:
            existing[k] = switch_config[k]
            changed = True
    if not changed:
        return True, "无需持久化"

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
        os.chmod(cfg_path, 0o600)
        return True, str(cfg_path)
    except Exception as e:
        return False, f"写入失败: {e}"


@router.post("/llm/validate")
async def validate_llm_key(req: dict = Body(...)):
    """
    校验 API Key 有效性 (不切换，仅探测)

    Body: {"provider": "claude", "api_key": "sk-ant-...", "model": "claude-sonnet-4-5"}
    """
    provider = req.get("provider", "")
    api_key = req.get("api_key", "")
    model = req.get("model", "")

    if provider == "claude":
        from api.server import AnthropicClient, DEFAULT_ANTHROPIC_MODEL
        fmt_ok, fmt_msg = AnthropicClient.validate_key_format(api_key)
        if not fmt_ok:
            return {"success": False, "stage": "format", "error": fmt_msg}
        client = AnthropicClient(api_key=api_key, model=model or DEFAULT_ANTHROPIC_MODEL)
        ok, msg = await client.validate()
        return {
            "success": ok,
            "stage": "network",
            "provider": "claude",
            "model": client.model,
            "error": None if ok else msg,
        }
    if provider == "deepseek":
        from api.server import DeepseekClient, DEFAULT_DEEPSEEK_MODEL
        fmt_ok, fmt_msg = DeepseekClient.validate_key_format(api_key)
        if not fmt_ok:
            return {"success": False, "stage": "format", "error": fmt_msg}
        client = DeepseekClient(api_key=api_key, model=model or DEFAULT_DEEPSEEK_MODEL)
        ok, msg = await client.validate()
        return {
            "success": ok,
            "stage": "network",
            "provider": "deepseek",
            "model": client.model,
            "error": None if ok else msg,
        }
    return {"success": False, "error": f"暂不支持校验 provider={provider}"}


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
        # 本地格式校验
        if api_key:
            from api.server import AnthropicClient
            ok, msg = AnthropicClient.validate_key_format(api_key)
            if not ok:
                return {"success": False, "error": f"API Key 校验失败: {msg}"}
            switch_config["anthropic_api_key"] = api_key
        if model:
            switch_config["model"] = model
        else:
            from api.server import DEFAULT_ANTHROPIC_MODEL
            switch_config["model"] = DEFAULT_ANTHROPIC_MODEL
        # 实网连通性校验（使用待切换的 key + model）
        if api_key:
            from api.server import AnthropicClient
            probe = AnthropicClient(api_key=api_key, model=switch_config["model"])
            ok, msg = await probe.validate()
            if not ok:
                return {"success": False, "error": f"Claude 连通性校验失败: {msg}"}
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
    elif provider == "deepseek":
        # DeepSeek — OpenAI 兼容端点
        switch_config["llm_provider"] = "deepseek"
        if api_key:
            from api.server import DeepseekClient
            ok, msg = DeepseekClient.validate_key_format(api_key)
            if not ok:
                return {"success": False, "error": f"API Key 校验失败: {msg}"}
            switch_config["deepseek_api_key"] = api_key
        from api.server import DEFAULT_DEEPSEEK_MODEL
        switch_config["model"] = model or DEFAULT_DEEPSEEK_MODEL
        if api_key:
            from api.server import DeepseekClient
            probe = DeepseekClient(api_key=api_key, model=switch_config["model"])
            ok, msg = await probe.validate()
            if not ok:
                return {"success": False, "error": f"DeepSeek 连通性校验失败: {msg}"}
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
    elif provider == "deepseek" and api_key:
        os.environ["DEEPSEEK_API_KEY"] = api_key

    try:
        new_client = _create_llm_client(switch_config)
        # 热替换 ReactAgent（主路径）
        if _react_agent:
            _react_agent.llm = new_client
            if hasattr(_react_agent, "_planner_compat"):
                _react_agent._planner_compat._llm = new_client
            for tool in _react_agent.tools._tools.values():
                if hasattr(tool, '_llm'):
                    tool._llm = new_client
                elif hasattr(tool, 'llm_client'):
                    tool.llm_client = new_client
        # planner / evaluator 单例（M3 后独立持有，不再走 _agent_loop）
        if _planner is not None:
            _planner._llm = new_client
        if _evaluator is not None:
            _evaluator._llm = new_client
        # 兼容：AgentLoop 路径（M3 退役后可删）
        if _agent_loop:
            _agent_loop.planner._llm = new_client
            _agent_loop.evaluator._llm = new_client
            if hasattr(_agent_loop, 'tool_registry'):
                for tool in _agent_loop.tool_registry._tools.values():
                    if hasattr(tool, '_llm'):
                        tool._llm = new_client
                    elif hasattr(tool, 'llm_client'):
                        tool.llm_client = new_client

        actual_provider = provider
        actual_model = getattr(new_client, "model", "mock")
        # 持久化（除 mock 外）：下次启动自动恢复此 provider + key + model
        persist_msg = ""
        if req.get("persist", True) and provider != "mock":
            ok, msg = _persist_llm_config(switch_config)
            persist_msg = f"，配置已保存到 {msg}" if ok else f"，但持久化失败: {msg}"
        return {
            "success": True,
            "provider": actual_provider,
            "model": actual_model,
            "message": f"已切换到 {actual_provider} ({actual_model}){persist_msg}",
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

        # 构建生成文件列表（去重：同一文件只保留最后一次变更）
        generated_files = []
        seen_paths = set()
        for c in reversed(code_changes):  # 从后往前，保留最新的
            # 统一路径：绝对路径转相对路径
            fp = c.file_path
            project = Path(_project_path).resolve()
            try:
                fp_resolved = Path(fp).resolve() if Path(fp).is_absolute() else (project / fp).resolve()
                rel = str(fp_resolved.relative_to(project))
            except (ValueError, OSError):
                rel = fp
            if rel not in seen_paths:
                seen_paths.add(rel)
                generated_files.append({
                    "path": c.file_path,
                    "action": c.action,
                    "lines": len((c.content or "").splitlines()),
                })
        generated_files.reverse()  # 恢复原始顺序

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
    from api.schemas import StepType

    tasks = _state_manager.list_tasks()
    records = []
    total = success = failed = running = 0

    for t in tasks:
        task_id = t.get("task_id", t.get("id", ""))
        status = _state_manager.get_task_status(task_id)
        if not status:
            continue

        # 从 checkpoint plan 中找 BUILD 步骤
        has_build = False
        if status.plan and status.plan.subtasks:
            has_build = any(s.step_type == StepType.BUILD for s in status.plan.subtasks)

        # 也检查日志
        if not has_build:
            logs = getattr(status, "logs", []) or []
            for log in logs:
                log_text = log if isinstance(log, str) else str(log)
                if "build" in log_text.lower() or "pack" in log_text.lower() or "打包" in log_text:
                    has_build = True
                    break

        if not has_build:
            continue

        total += 1
        task_status_val = getattr(status, "status", None)
        s = task_status_val.value if task_status_val and hasattr(task_status_val, "value") else str(task_status_val or "pending")
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
            "id": task_id,
            "task": t.get("description", task_id),
            "artifact": "-",
            "size": "-",
            "status": badge,
            "start_time": t.get("created_at", "-"),
            "duration": "-",
        })

    # ★ 扫描磁盘上的构建产物
    artifacts = _scan_build_artifacts()

    # ★ 收集候选版本（优先 _version_manager，回退 _agent_loop.versions）
    candidates = []
    vm = _version_manager or (_agent_loop.versions if _agent_loop else None)
    if vm:
        for t in tasks:
            task_id = t.get("task_id", t.get("id", ""))
            task_candidates = vm.get_candidates(task_id)
            for c in task_candidates:
                candidates.append({
                    "version_id": c.version_id,
                    "version_number": c.version_number,
                    "task_id": task_id,
                    "ai_confidence": c.ai_confidence,
                    "ai_summary": c.ai_summary,
                    "status": c.status.value,
                    "build_success": c.build_success,
                    "created_at": c.created_at.isoformat() if hasattr(c, "created_at") and c.created_at else "-",
                })

    return {
        "total": total, "success": success, "failed": failed, "running": running,
        "records": records,
        "artifacts": artifacts,
        "candidates": candidates,
    }


def _scan_build_artifacts() -> list[dict]:
    """扫描项目目录中的构建产物"""
    project = Path(_project_path)
    if not project.is_dir():
        return []

    # 常见构建产物扩展名
    artifact_patterns = [
        "*.nro", "*.nso", "*.elf", "*.bin", "*.hex",  # Switch / embedded
        "*.exe", "*.dll", "*.so", "*.dylib", "*.a",    # native
        "*.jar", "*.war",                               # Java
        "*.whl", "*.egg",                               # Python
        "*.zip", "*.tar.gz", "*.tgz",                   # archives
        "*.deb", "*.rpm", "*.apk",                      # packages
        "*.wasm",                                       # WebAssembly
    ]
    skip_dirs = {".kedo", ".git", ".venv", "node_modules", "__pycache__"}

    artifacts = []
    for pattern in artifact_patterns:
        for item in project.rglob(pattern):
            parts = item.relative_to(project).parts
            if any(p in skip_dirs for p in parts):
                continue
            try:
                stat = item.stat()
                size_bytes = stat.st_size
                # 格式化大小
                if size_bytes < 1024:
                    size_str = f"{size_bytes} B"
                elif size_bytes < 1024 * 1024:
                    size_str = f"{size_bytes / 1024:.1f} KB"
                else:
                    size_str = f"{size_bytes / (1024*1024):.1f} MB"

                from datetime import datetime
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

                artifacts.append({
                    "name": item.name,
                    "path": str(item.relative_to(project)),
                    "size": size_str,
                    "size_bytes": size_bytes,
                    "modified": mtime,
                })
            except (OSError, PermissionError):
                continue

    artifacts.sort(key=lambda a: a.get("modified", ""), reverse=True)
    return artifacts

@router.get("/deploy/guide")
async def get_deploy_guide():
    """
    从部署文档（docs/deploy/deployment.md）解析部署指南。
    优先使用缓存的 deploy_manifest.json，否则从文档解析并缓存。
    """
    project = Path(_project_path)
    manifest_path = project / ".kedo" / "deploy_manifest.json"

    # 优先读取缓存
    if manifest_path.exists():
        try:
            import json as _json
            cached = _json.loads(manifest_path.read_text(encoding="utf-8"))
            # 检查部署文档是否更新了
            deploy_path = project / "docs" / "deploy" / "deployment.md"
            if deploy_path.exists():
                doc_mtime = deploy_path.stat().st_mtime
                if cached.get("_doc_mtime", 0) >= doc_mtime:
                    cached["project_path"] = str(project.resolve())
                    cached["from_cache"] = True
                    return cached
        except Exception:
            pass

    # 读取部署文档
    deploy_doc = ""
    deploy_path = project / "docs" / "deploy" / "deployment.md"
    if deploy_path.exists():
        try:
            deploy_doc = deploy_path.read_text(encoding="utf-8")
        except Exception:
            pass

    # 读取需求文档获取项目描述
    req_doc = ""
    for req_path in [project / "docs" / "requirement" / "requirement.md", project / ".kedo" / "requirement.md"]:
        if req_path.exists():
            try:
                req_doc = req_path.read_text(encoding="utf-8")[:1500]
            except Exception:
                pass
            break

    # 扫描项目文件类型
    project_files = [str(f.relative_to(project)) for f in project.rglob("*") if f.is_file()
                     and not any(p in f.relative_to(project).parts for p in {".kedo", ".git", ".venv", "node_modules", "__pycache__"})][:50]

    guide = _parse_deploy_guide(deploy_doc, req_doc, project_files, project)

    # 缓存
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        guide["_doc_mtime"] = deploy_path.stat().st_mtime if deploy_path.exists() else 0
        manifest_path.write_text(_json.dumps(guide, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    guide["project_path"] = str(project.resolve())
    guide["from_cache"] = False
    return guide


def _parse_deploy_guide(deploy_doc: str, req_doc: str, project_files: list, project: Path) -> dict:
    """从部署文档解析出结构化部署指南"""
    import re

    # 从文档中提取章节
    sections = {}
    current_section = ""
    current_content = []
    for line in deploy_doc.split("\n"):
        if line.startswith("## "):
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = line[3:].strip()
            current_content = []
        else:
            current_content.append(line)
    if current_section:
        sections[current_section] = "\n".join(current_content).strip()

    # 从需求文档提取项目描述
    project_desc = ""
    if req_doc:
        lines = req_doc.split("\n")
        for line in lines[:20]:
            if line.strip() and not line.startswith("#"):
                project_desc = line.strip()
                break

    # 检测部署目标平台
    all_text = (deploy_doc + " " + req_doc).lower()
    file_list_str = " ".join(project_files).lower()

    if "switch" in all_text or ".nro" in file_list_str or "devkitpro" in all_text:
        platform = "Nintendo Switch"
        icon = "\U0001F3AE"
        platform_desc = "将编译后的 .nro 自制程序部署到 Nintendo Switch 游戏机上运行"
    elif "android" in all_text or ".apk" in file_list_str:
        platform = "Android"
        icon = "\U0001F4F1"
        platform_desc = "将 APK 安装到 Android 设备"
    elif "ios" in all_text or "xcode" in all_text:
        platform = "iOS"
        icon = "\U0001F34E"
        platform_desc = "将应用部署到 iOS 设备"
    elif "docker" in all_text or "docker-compose.yml" in file_list_str:
        platform = "Docker / 服务器"
        icon = "\U0001F433"
        platform_desc = "通过 Docker 容器化部署到服务器"
    elif "package.json" in file_list_str:
        platform = "Node.js"
        icon = "\U0001F7E9"
        platform_desc = "部署 Node.js 应用到服务器"
    elif "go.mod" in file_list_str:
        platform = "Go"
        icon = "\U0001F4E6"
        platform_desc = "编译 Go 应用并部署到服务器"
    else:
        platform = "通用"
        icon = "\U0001F4E6"
        platform_desc = "构建并部署到目标环境"

    # 从部署文档的章节中提取步骤
    steps = []
    step_num = 0

    # 部署架构 → 概述
    arch_section = sections.get("部署架构", sections.get("Deployment Architecture", ""))

    # 环境配置 → 准备步骤
    env_section = sections.get("环境配置", sections.get("Environment Configuration",
                  sections.get("环境要求", "")))

    # 部署流程 → 执行步骤
    flow_section = sections.get("部署流程", sections.get("Deployment Process",
                   sections.get("部署步骤", "")))

    # 监控和调试
    monitor_section = sections.get("监控与调试计划", sections.get("Monitoring",
                      sections.get("监控和调试", sections.get("监控与调试方案", ""))))

    # 如果文档有内容，从中提取步骤
    if env_section or flow_section:
        # 提取列表项作为步骤
        for section_name, section_content in [("环境准备", env_section), ("部署执行", flow_section), ("监控调试", monitor_section)]:
            if not section_content:
                continue
            # 解析 markdown 列表项和子标题
            items = re.findall(r'(?:^|\n)(?:[-*]|\d+\.)\s+(.+)', section_content)
            sub_titles = re.findall(r'(?:^|\n)###?\s+(.+)', section_content)

            if sub_titles:
                for title in sub_titles:
                    step_num += 1
                    # 获取该子标题下的内容
                    idx = section_content.find(title)
                    next_idx = len(section_content)
                    for st in sub_titles:
                        si = section_content.find(st)
                        if si > idx and si < next_idx:
                            next_idx = si
                    sub_content = section_content[idx + len(title):next_idx].strip()
                    sub_items = re.findall(r'(?:^|\n)(?:[-*]|\d+\.)\s+(.+)', sub_content)

                    steps.append({
                        "step": step_num,
                        "title": title.strip(),
                        "description": "\n".join(sub_items[:3]) if sub_items else sub_content[:150],
                        "commands": [item.strip() for item in sub_items if item.strip().startswith(("apt", "docker", "make", "npm", "pip", "curl", "wget", "git", "scp", "ssh", "nxlink", "dkp-"))],
                        "manual": section_name != "部署执行",
                        "status": "todo",
                    })
            elif items:
                step_num += 1
                steps.append({
                    "step": step_num,
                    "title": section_name,
                    "description": "\n".join(items[:5]),
                    "commands": [item.strip() for item in items if any(item.strip().startswith(cmd) for cmd in ("apt", "docker", "make", "npm", "pip", "curl", "wget", "git", "scp", "ssh", "nxlink", "dkp-"))],
                    "manual": section_name != "部署执行",
                    "status": "todo",
                })

    # 如果文档太短或没有解析出步骤，用文件检测生成基本步骤
    if not steps:
        has_dockerfile = (project / "docker-compose.yml").exists() or (project / "Dockerfile").exists()
        has_makefile = (project / "Makefile").exists()

        steps = [
            {
                "step": 1,
                "title": "搭建编译环境",
                "description": "安装项目所需的编译工具链和依赖" + ("\n已检测到 Docker 配置文件，推荐使用 Docker" if has_dockerfile else ""),
                "commands": ["docker-compose build"] if has_dockerfile else [],
                "manual": not has_dockerfile,
                "status": "ready" if has_dockerfile else "todo",
            },
            {
                "step": 2,
                "title": "编译构建",
                "description": "编译源代码生成可执行文件或部署包",
                "commands": ["make"] if has_makefile else (["docker-compose run --rm build make"] if has_dockerfile else []),
                "manual": False,
                "status": "ready" if has_makefile or has_dockerfile else "blocked",
                "note": "需要先创建构建脚本（Makefile 等）" if not has_makefile and not has_dockerfile else None,
            },
            {
                "step": 3,
                "title": "准备目标设备/环境",
                "description": "确保部署目标（设备、服务器、容器环境）已就绪",
                "commands": [],
                "manual": True,
                "status": "manual",
            },
            {
                "step": 4,
                "title": "部署",
                "description": "将构建产物传输到目标环境并启动",
                "commands": [],
                "manual": True,
                "status": "manual",
            },
            {
                "step": 5,
                "title": "验证",
                "description": "在目标环境上验证功能是否正常",
                "commands": [],
                "manual": True,
                "status": "manual",
            },
        ]

    # 构建需求列表
    requirements = []
    if "switch" in platform.lower():
        requirements = [
            "一台已破解的 Nintendo Switch 游戏机（Atmosphere 自定义固件）",
            "一台编译服务器（已安装 Docker 或 devkitPro）",
            "Switch 和服务器在同一局域网（用于 WiFi 部署）或 SD 卡读卡器",
        ]
    elif "docker" in platform.lower() or "服务器" in platform.lower():
        requirements = ["目标服务器（已安装 Docker）", "SSH 访问权限"]
    elif "android" in platform.lower():
        requirements = ["Android 设备（开启开发者模式）", "USB 数据线或 ADB WiFi"]
    else:
        requirements = ["目标运行环境", "网络或物理访问权限"]

    return {
        "target": {
            "platform": platform,
            "description": platform_desc,
            "icon": icon,
            "project_description": project_desc,
            "requirements": requirements,
        },
        "steps": steps,
        "deploy_doc_exists": bool(deploy_doc),
        "deploy_doc_sections": list(sections.keys()),
    }


@router.get("/deploy/auto-env")
async def get_auto_detected_env():
    """获取自动检测到的环境变量"""
    env_path = Path(_project_path) / ".kedo" / "env_auto_detected.json"
    if env_path.exists():
        try:
            return json.loads(env_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


@router.post("/server/restart")
async def restart_server():
    """重启 kedo server（通过退出进程，依赖外部 supervisor 重启）"""
    import signal
    # 发送信号给自身进程，让 uvicorn 优雅退出
    # 外部的 nohup 或 systemd 会自动重启
    os.kill(os.getpid(), signal.SIGTERM)
    return {"status": "restarting"}


@router.get("/deploy/environment")
async def get_deploy_environment():
    """检测部署环境依赖状态，标记哪些需要人工准备"""
    import subprocess
    import shutil

    project = Path(_project_path)
    checks = []

    def check_cmd(name, cmd, desc, install_hint):
        """检查命令是否可用"""
        found = shutil.which(cmd) is not None
        version = ""
        if found:
            try:
                r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
                version = (r.stdout or r.stderr or "").strip().split("\n")[0][:80]
            except Exception:
                version = "installed"
        checks.append({
            "name": name,
            "command": cmd,
            "description": desc,
            "status": "ready" if found else "missing",
            "version": version,
            "install_hint": install_hint if not found else "",
            "manual": not found,
        })

    def check_file(name, path, desc, hint):
        """检查文件是否存在"""
        exists = Path(path).exists() if not path.startswith("/") else Path(path).exists()
        # 相对于项目目录
        if not Path(path).is_absolute():
            exists = (project / path).exists()
        checks.append({
            "name": name,
            "path": path,
            "description": desc,
            "status": "ready" if exists else "missing",
            "install_hint": hint if not exists else "",
            "manual": not exists,
        })

    # ---- 编译工具链 ----
    check_cmd("Docker", "docker", "容器化构建环境", "curl -fsSL https://get.docker.com | sh")
    check_cmd("Docker Compose", "docker-compose", "容器编排", "apt install docker-compose 或 pip install docker-compose")
    check_cmd("Make", "make", "构建工具", "apt install build-essential")
    check_cmd("Git", "git", "版本控制", "apt install git")

    # ---- devkitPro (Switch 交叉编译) ----
    devkitpro = os.environ.get("DEVKITPRO", "/opt/devkitpro")
    check_file("devkitPro", devkitpro, "Switch 交叉编译工具链",
               "参考 https://devkitpro.org/wiki/Getting_Started 安装\n"
               "或使用 Docker: docker-compose build")
    check_cmd("nxlink", "nxlink", "Switch WiFi 部署工具 (devkitPro)",
              "安装 devkitPro 后自动包含，或 dkp-pacman -S switch-tools")

    # ---- 项目文件 ----
    check_file("Dockerfile", "docker-compose.yml", "Docker 构建配置", "kedo 应自动生成此文件")
    check_file("Makefile", "Makefile", "项目构建脚本", "kedo 应自动生成此文件")
    check_file("源代码", "source/main.cpp", "项目主入口", "kedo 应自动生成代码")

    # ---- 部署目标 ----
    check_file("构建产物", "switchvideo.nro", "Switch 可执行文件",
               "执行 make 或 docker-compose run build 生成")

    # 统计
    ready_count = sum(1 for c in checks if c["status"] == "ready")
    missing_count = sum(1 for c in checks if c["status"] == "missing")
    manual_count = sum(1 for c in checks if c.get("manual"))

    return {
        "checks": checks,
        "ready": ready_count,
        "missing": missing_count,
        "manual_required": manual_count,
        "all_ready": missing_count == 0,
        "project_path": str(project.resolve()),
    }


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
    planner = _planner or (_agent_loop.planner if _agent_loop and hasattr(_agent_loop, "planner") else None)
    if planner is not None:
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
    evaluator = _evaluator or (_agent_loop.evaluator if _agent_loop and hasattr(_agent_loop, "evaluator") else None)
    if evaluator is not None:
        llm_name = type(getattr(evaluator, "_llm", None)).__name__ if hasattr(evaluator, "_llm") else "unknown"
        agents.append({
            "name": "Evaluator",
            "type": llm_name,
            "stage": "测试评估 / 质量检查",
            "status": "success",
            "task_count": 0,
            "last_activity": "-",
        })

    # Tool agents — 优先从 ReactAgent 拿，回退到旧 AgentLoop
    registry = None
    if _react_agent and hasattr(_react_agent, "tools"):
        registry = _react_agent.tools
    elif _agent_loop and hasattr(_agent_loop, "tool_registry"):
        registry = _agent_loop.tool_registry
    if registry is not None:
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


# ============================================================
# 部署目标管理 API (deploy targets CRUD + check + setup)
# ============================================================

def _deploy_targets_path() -> Path:
    """获取 deploy_targets.json 路径"""
    return Path(_project_path) / ".kedo" / "deploy_targets.json"


def _load_deploy_targets() -> list[dict]:
    """从磁盘加载部署目标列表。如果为空，自动从部署文档解析。"""
    fp = _deploy_targets_path()
    if fp.exists():
        try:
            targets = json.loads(fp.read_text(encoding="utf-8"))
            if targets:
                return targets
        except Exception:
            pass

    # 没有手动添加的 targets → 从部署文档自动解析
    auto_targets = _auto_detect_deploy_targets()
    if auto_targets:
        _save_deploy_targets(auto_targets)
    return auto_targets


def _auto_detect_deploy_targets() -> list[dict]:
    """从部署文档和项目文件自动检测需要的部署环境"""
    project = Path(_project_path)
    targets = []

    # 读取部署文档
    deploy_doc = ""
    deploy_path = project / "docs" / "deploy" / "deployment.md"
    if deploy_path.exists():
        try:
            deploy_doc = deploy_path.read_text(encoding="utf-8").lower()
        except Exception:
            pass

    # 读取需求文档
    req_doc = ""
    req_path = project / "docs" / "requirement" / "requirement.md"
    if req_path.exists():
        try:
            req_doc = req_path.read_text(encoding="utf-8").lower()
        except Exception:
            pass

    all_text = deploy_doc + " " + req_doc
    file_list = " ".join(str(f.name) for f in project.rglob("*") if f.is_file())

    # 检测项目类型 → 自动生成对应的部署环境
    has_switch = "switch" in all_text or ".nro" in file_list or "devkitpro" in all_text or "libnx" in all_text
    has_docker = "docker" in all_text or (project / "docker-compose.yml").exists() or (project / "Dockerfile").exists()
    has_server = "服务器" in all_text or "server" in all_text or "ssh" in all_text or "scp" in all_text
    has_android = "android" in all_text or ".apk" in file_list
    has_ios = "ios" in all_text or "xcode" in all_text

    now = datetime.now(timezone.utc).isoformat()

    # 编译服务器（几乎所有项目都需要）
    if has_docker or has_switch or (project / "Makefile").exists() or (project / "CMakeLists.txt").exists():
        targets.append({
            "id": str(uuid.uuid4())[:8],
            "name": "\u7F16\u8BD1\u670D\u52A1\u5668",  # 编译服务器
            "type": "server",
            "ip": "",
            "port": 22,
            "user": "",
            "status": "unconfigured",
            "setup_commands": [],
            "created_at": now,
            "last_check": None,
            "auto_detected": True,
            "description": "\u7528\u4E8E\u7F16\u8BD1\u6784\u5EFA\u9879\u76EE\u7684\u670D\u52A1\u5668\uFF0C\u9700\u8981\u5B89\u88C5\u7F16\u8BD1\u5DE5\u5177\u94FE",
            # 用于编译构建项目的服务器，需要安装编译工具链
        })

    # Switch 游戏机
    if has_switch:
        targets.append({
            "id": str(uuid.uuid4())[:8],
            "name": "Nintendo Switch",
            "type": "switch",
            "ip": "",
            "port": 0,
            "user": "",
            "status": "unconfigured",
            "setup_commands": [],
            "created_at": now,
            "last_check": None,
            "auto_detected": True,
            "description": "\u76EE\u6807\u8FD0\u884C\u8BBE\u5907\uFF0C\u9700\u8981 Atmosphere \u81EA\u5B9A\u4E49\u56FA\u4EF6\u548C hbmenu",
            # 目标运行设备，需要 Atmosphere 自定义固件和 hbmenu
            "checklist": [
                {"item": "\u5B89\u88C5 Atmosphere \u81EA\u5B9A\u4E49\u56FA\u4EF6", "done": False},
                # 安装 Atmosphere 自定义固件
                {"item": "SD \u5361\u4E0A\u6709 /switch/ \u76EE\u5F55", "done": False},
                # SD 卡上有 /switch/ 目录
                {"item": "Switch \u5DF2\u8FDE\u63A5 WiFi\uFF08\u540C\u4E00\u5C40\u57DF\u7F51\uFF09", "done": False},
                # Switch 已连接 WiFi（同一局域网）
                {"item": "\u8BB0\u5F55 Switch IP \u5730\u5740", "done": False},
                # 记录 Switch IP 地址
            ],
        })

    # Android 设备
    if has_android:
        targets.append({
            "id": str(uuid.uuid4())[:8],
            "name": "Android \u8BBE\u5907",
            "type": "android",
            "ip": "",
            "port": 5555,
            "user": "",
            "status": "unconfigured",
            "setup_commands": [],
            "created_at": now,
            "last_check": None,
            "auto_detected": True,
            "description": "\u76EE\u6807 Android \u8BBE\u5907\uFF0C\u9700\u8981\u5F00\u542F\u5F00\u53D1\u8005\u6A21\u5F0F",
        })

    return targets


def _save_deploy_targets(targets: list[dict]):
    """保存部署目标列表到磁盘"""
    fp = _deploy_targets_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/deploy/targets")
async def list_deploy_targets():
    """列出所有部署目标"""
    return _load_deploy_targets()


@router.post("/deploy/targets")
async def add_deploy_target(body: dict = Body(...)):
    """添加新的部署目标"""
    targets = _load_deploy_targets()
    target = {
        "id": str(uuid.uuid4())[:8],
        "name": body.get("name", "未命名"),
        "type": body.get("type", "server"),
        "ip": body.get("ip", ""),
        "port": body.get("port", 22),
        "user": body.get("user", "root"),
        "status": "unknown",
        "setup_commands": body.get("setup_commands", []),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_check": None,
    }
    targets.append(target)
    _save_deploy_targets(targets)
    return target


@router.put("/deploy/targets/{target_id}")
async def update_deploy_target(target_id: str, body: dict = Body(...)):
    """更新部署目标"""
    targets = _load_deploy_targets()
    for t in targets:
        if t["id"] == target_id:
            for key in ("name", "type", "ip", "port", "user", "setup_commands", "status", "checklist", "description"):
                if key in body:
                    t[key] = body[key]
            _save_deploy_targets(targets)
            return t
    raise HTTPException(404, f"Deploy target {target_id} not found")


@router.delete("/deploy/targets/{target_id}")
async def delete_deploy_target(target_id: str):
    """删除部署目标"""
    targets = _load_deploy_targets()
    new_targets = [t for t in targets if t["id"] != target_id]
    if len(new_targets) == len(targets):
        raise HTTPException(404, f"Deploy target {target_id} not found")
    _save_deploy_targets(new_targets)
    return {"status": "ok", "message": f"Target {target_id} deleted"}


@router.post("/deploy/targets/{target_id}/check")
async def check_deploy_target(target_id: str):
    """检测部署目标的可达性（ping + SSH）"""
    targets = _load_deploy_targets()
    target = None
    for t in targets:
        if t["id"] == target_id:
            target = t
            break
    if not target:
        raise HTTPException(404, f"Deploy target {target_id} not found")

    ip = target.get("ip", "")
    user = target.get("user", "root")
    port = target.get("port", 22)

    # Update status to checking
    target["status"] = "checking"
    target["last_check"] = datetime.now(timezone.utc).isoformat()
    _save_deploy_targets(targets)

    result = {"ping": False, "ssh": False, "detail": ""}

    # Step 1: ping
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "2", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        result["ping"] = proc.returncode == 0
        if not result["ping"]:
            result["detail"] = f"Ping failed: {stderr.decode(errors='replace').strip()}"
    except asyncio.TimeoutError:
        result["detail"] = "Ping timeout"
    except Exception as e:
        result["detail"] = f"Ping error: {e}"

    # Step 2: SSH test (only if ping succeeded)
    if result["ping"]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=3",
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                "-p", str(port),
                f"{user}@{ip}", "echo", "ok",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
            result["ssh"] = proc.returncode == 0 and "ok" in stdout.decode(errors="replace")
            if not result["ssh"]:
                result["detail"] = f"SSH failed: {stderr.decode(errors='replace').strip()}"
            else:
                result["detail"] = "SSH connection OK"
        except asyncio.TimeoutError:
            result["detail"] = "SSH timeout"
        except Exception as e:
            result["detail"] = f"SSH error: {e}"

    # Update status
    if result["ssh"]:
        target["status"] = "reachable"
    elif result["ping"]:
        target["status"] = "unreachable"
    else:
        target["status"] = "unreachable"
    _save_deploy_targets(targets)

    result["status"] = target["status"]
    return result


def _extract_setup_commands_from_doc() -> list[str]:
    """从部署文档中提取可执行的 setup 命令"""
    project = Path(_project_path)
    deploy_path = project / "docs" / "deploy" / "deployment.md"
    if not deploy_path.exists():
        return []
    try:
        doc = deploy_path.read_text(encoding="utf-8")
    except Exception:
        return []

    commands = []
    cmd_prefixes = ("apt", "apt-get", "docker", "make", "pip", "pip3", "npm", "yarn",
                    "curl", "wget", "git", "scp", "ssh", "dkp-pacman", "sudo",
                    "mkdir", "chmod", "chown", "cp", "mv", "ln", "export")
    in_code_block = False
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            if stripped and any(stripped.startswith(p) for p in cmd_prefixes):
                commands.append(stripped)
        else:
            if stripped.startswith("$ "):
                cmd = stripped[2:].strip()
                if cmd and any(cmd.startswith(p) for p in cmd_prefixes):
                    commands.append(cmd)
    return commands


@router.post("/deploy/targets/{target_id}/setup")
async def setup_deploy_target(target_id: str, body: dict = Body(default={})):
    """在部署目标上执行 setup 命令（通过 SSH）"""
    targets = _load_deploy_targets()
    target = None
    for t in targets:
        if t["id"] == target_id:
            target = t
            break
    if not target:
        raise HTTPException(404, f"Deploy target {target_id} not found")

    ip = target.get("ip", "")
    user = target.get("user", "root")
    port = target.get("port", 22)

    # Get commands: from body, from target, or from deploy doc
    commands = body.get("commands") or target.get("setup_commands") or []
    if not commands:
        commands = _extract_setup_commands_from_doc()
    if not commands:
        return {"status": "error", "message": "没有可执行的 setup 命令", "logs": []}

    # Update status
    target["status"] = "setting_up"
    _save_deploy_targets(targets)

    # Publish start event
    if _state_manager:
        try:
            _state_manager.event_bus.publish(
                event_type="deploy_setup_start",
                task_id="deploy",
                data={"target_id": target_id, "target_name": target["name"], "commands": commands},
            )
        except Exception:
            pass

    logs = []
    all_success = True

    for i, cmd in enumerate(commands):
        log_entry = {"index": i, "command": cmd, "stdout": "", "stderr": "", "returncode": -1}
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=no",
                "-p", str(port),
                f"{user}@{ip}", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            log_entry["stdout"] = stdout.decode(errors="replace")
            log_entry["stderr"] = stderr.decode(errors="replace")
            log_entry["returncode"] = proc.returncode

            if proc.returncode != 0:
                all_success = False
                log_entry["status"] = "failed"
            else:
                log_entry["status"] = "success"
        except asyncio.TimeoutError:
            log_entry["stderr"] = "Command timed out (120s)"
            log_entry["status"] = "timeout"
            all_success = False
        except Exception as e:
            log_entry["stderr"] = str(e)
            log_entry["status"] = "error"
            all_success = False

        logs.append(log_entry)

        # Publish progress event
        if _state_manager:
            try:
                _state_manager.event_bus.publish(
                    event_type="deploy_setup_progress",
                    task_id="deploy",
                    data={
                        "target_id": target_id,
                        "index": i,
                        "total": len(commands),
                        "command": cmd,
                        "status": log_entry["status"],
                        "stdout": log_entry["stdout"][-500:],
                        "stderr": log_entry["stderr"][-500:],
                    },
                )
            except Exception:
                pass

        # Stop on failure
        if not all_success:
            break

    # Update final status
    target["status"] = "ready" if all_success else "setup_failed"
    _save_deploy_targets(targets)

    # Publish completion event
    if _state_manager:
        try:
            _state_manager.event_bus.publish(
                event_type="deploy_setup_complete",
                task_id="deploy",
                data={
                    "target_id": target_id,
                    "target_name": target["name"],
                    "status": target["status"],
                    "total_commands": len(commands),
                    "executed": len(logs),
                },
            )
        except Exception:
            pass

    return {
        "status": target["status"],
        "message": "环境搭建完成" if all_success else "环境搭建失败",
        "logs": logs,
    }


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
