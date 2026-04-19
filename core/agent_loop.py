"""
DEPRECATED — core.agent_loop has been retired (P3-M3).

新流量请用 `from core.react_agent import ReactAgent`。

此模块作为兼容 shim 保留：旧导入 `from core.agent_loop import AgentLoop` 继续可用，
但会发出 DeprecationWarning。计划在 4-6 周后（实战确认无回归）从 _legacy/ 真删。
"""
from __future__ import annotations

import warnings

warnings.warn(
    "core.agent_loop is deprecated and only kept as a compatibility shim. "
    "Use core.react_agent.ReactAgent instead. "
    "The legacy module will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

# 重导出旧符号（让 `from core.agent_loop import AgentLoop` 等继续工作）
from core._legacy.agent_loop import *  # noqa: F401, F403, E402
