"""Shared imports and constants for project orchestration services."""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.project import (
    Project,
    ProjectAccessGrant,
    ProjectCapabilityBinding,
    ProjectEvent,
    ProjectMemberSnapshot,
    ProjectRun,
    ProjectRunMemberSnapshot,
    ProjectWorkItem,
)
from app.models.skill import Skill
from app.models.subagent_run import SubagentRun
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.schemas.project import (
    ProjectAgentCreate,
    ProjectCapabilityCreate,
    ProjectCreate,
    ProjectMemberCreate,
)
from app.services.project_capability_options import load_project_capability_options

PROJECT_EVENT_SUMMARY_MAX_LENGTH = 500
PROJECT_RUNTIME_STATUS_RUNNING = "running"
PROJECT_RUNTIME_STATUS_PAUSED = "paused"
TERMINAL_PROJECT_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
PROJECT_CONVERSATION_STATUSES = frozenset(
    {
        "planning",
        PROJECT_RUNTIME_STATUS_RUNNING,
        PROJECT_RUNTIME_STATUS_PAUSED,
        "waiting",
        "completed",
    }
)
