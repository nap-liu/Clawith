"""Ordered aggregate of all built-in agent tool schemas."""

from app.services.agent_tools_catalog_core import AGENT_TOOL_CORE
from app.services.agent_tools_catalog_feishu import AGENT_TOOL_FEISHU
from app.services.agent_tools_catalog_integrations import AGENT_TOOL_INTEGRATIONS
from app.services.agent_tools_catalog_runtime import AGENT_TOOL_RUNTIME
from app.services.media_ai_contract import MEDIA_AI_FUNCTIONS
from app.services.model_catalog_tool import MODEL_CATALOG_FUNCTION
from app.services.image_context_tool import IMAGE_CONTEXT_FUNCTION_TOOL

AGENT_TOOLS = [
    *MEDIA_AI_FUNCTIONS,
    IMAGE_CONTEXT_FUNCTION_TOOL,
    MODEL_CATALOG_FUNCTION,
    *AGENT_TOOL_CORE,
    *AGENT_TOOL_RUNTIME,
    *AGENT_TOOL_FEISHU,
    *AGENT_TOOL_INTEGRATIONS,
]

__all__ = ["AGENT_TOOLS"]
