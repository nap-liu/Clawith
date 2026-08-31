"""Ordered aggregate of all built-in agent tool schemas."""

from app.services.agent_tools_catalog_core import AGENT_TOOL_CORE
from app.services.agent_tools_catalog_feishu import AGENT_TOOL_FEISHU
from app.services.agent_tools_catalog_integrations import AGENT_TOOL_INTEGRATIONS
from app.services.agent_tools_catalog_runtime import AGENT_TOOL_RUNTIME

AGENT_TOOLS = [
    *AGENT_TOOL_CORE,
    *AGENT_TOOL_RUNTIME,
    *AGENT_TOOL_FEISHU,
    *AGENT_TOOL_INTEGRATIONS,
]

__all__ = ["AGENT_TOOLS"]

