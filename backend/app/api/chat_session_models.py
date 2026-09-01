"""Response models for chat-session API endpoints."""

from typing import Literal, Optional

from pydantic import BaseModel


class SessionOut(BaseModel):
    id: str
    agent_id: str
    user_id: Optional[str] = None
    username: Optional[str] = None      # display_name ?? username
    source_channel: str = "web"         # web / feishu / discord / slack / agent
    title: str
    created_at: str
    last_message_at: Optional[str] = None
    message_count: int = 0
    unread_count: int = 0
    is_primary: bool = False
    # Agent-to-agent session fields
    peer_agent_id: Optional[str] = None
    peer_agent_name: Optional[str] = None
    participant_type: str = "user"       # 'user' | 'agent'
    # Group chat session fields
    is_group: bool = False
    group_name: Optional[str] = None

    class Config:
        from_attributes = True

class SessionRuntimeOut(BaseModel):
    kind: Literal["subagent"]
    status: str
    execution_agent_id: str
    execution_agent_name: str
    mode: str
    model: Optional[str] = None
    soul: bool = True
    memory: bool = True

class SessionDetailOut(SessionOut):
    view_scope: Literal["mine", "all"]
    runtime: Optional[SessionRuntimeOut] = None

class SessionPageOut(BaseModel):
    items: list[SessionOut]
    has_more: bool
    next_offset: Optional[int] = None
    next_cursor: Optional[str] = None
