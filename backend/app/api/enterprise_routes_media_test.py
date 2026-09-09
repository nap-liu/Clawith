"""Model tests reuse the existing durable media executor and session viewer."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi import Depends, HTTPException
from jsonschema import FormatChecker, ValidationError, validate
from pydantic import BaseModel, Field

from app.api.enterprise_api_shared import router
from app.core.permissions import check_agent_access
from app.core.security import get_current_admin
from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.user import User
from app.services.llm.failure_outcome import render_message
from app.services.media_ai_contract import GENERATE_MEDIA_SCHEMA, READ_MEDIA_SCHEMA
from app.services.media_ai_io import MediaAIError
from app.services.media_ai_sessions import enqueue_media
from app.services.media_model_selection import model_connection
from app.services.model_capabilities import MEDIA_MODEL_DEFAULTS, ModelPurpose, supports_purpose


class MediaModelTestRequest(BaseModel):
    agent_id: uuid.UUID
    purpose: ModelPurpose
    prompt: str = Field(min_length=1)
    files: list[str | dict] | None = None


@router.post("/llm-models/{model_id}/media-test")
async def test_media_model(
    model_id: uuid.UUID,
    data: MediaModelTestRequest,
    current_user: User = Depends(get_current_admin),
):
    if data.purpose == "speech_recognition" or data.purpose not in MEDIA_MODEL_DEFAULTS.values():
        raise HTTPException(422, detail=render_message("mediaAI.invalidArguments"))
    tool = "read_media" if data.purpose == "media_understanding" else "generate_media"
    arguments = {"prompt": data.prompt.strip(), "model_id": str(model_id)}
    if data.files is not None:
        arguments["files"] = data.files
    if tool == "generate_media":
        arguments["output_type"] = data.purpose.removesuffix("_generation")
    elif not data.files:
        raise HTTPException(422, detail=render_message("mediaAI.inputCombination"))
    try:
        validate(arguments, READ_MEDIA_SCHEMA if tool == "read_media" else GENERATE_MEDIA_SCHEMA,
                 format_checker=FormatChecker())
        async with async_session() as db:
            agent, access = await check_agent_access(db, current_user, data.agent_id)
            model = await db.get(LLMModel, model_id)
            if (access == "read" or agent.agent_type != "native" or agent.scope != "standard"
                    or agent.tenant_id != current_user.tenant_id
                    or model is None or model.tenant_id != agent.tenant_id
                    or not model.enabled or not supports_purpose(model, data.purpose)):
                raise HTTPException(403, detail=render_message("mediaAI.modelUnavailable"))
            config = model_connection(model)
            parent = ChatSession(agent_id=agent.id, user_id=current_user.id,
                                 source_channel="web", title=data.prompt[:200])
            db.add(parent)
            await db.flush()
            anchor = ChatMessage(agent_id=agent.id, user_id=current_user.id, role="user",
                                 conversation_id=str(parent.id), content=arguments["prompt"],
                                 message_meta={"kind": "media_model_test"})
            db.add(anchor)
            await db.flush()
            state = SimpleNamespace(
                agent_id=agent.id, user_id=current_user.id, session_id=str(parent.id),
                turn_anchor_id=anchor.id, tool_call_id=f"model-test-{anchor.id}",
                tool_name=tool, arguments=arguments,
            )
            await db.commit()
        receipt = await enqueue_media(state, config, notify_parent=False)
        return {**receipt, "agent_id": str(data.agent_id)}
    except ValidationError as exc:
        raise HTTPException(422, detail=render_message("mediaAI.invalidArguments")) from exc
    except MediaAIError as exc:
        raise HTTPException(422, detail=render_message(f"mediaAI.{exc.code}")) from exc
