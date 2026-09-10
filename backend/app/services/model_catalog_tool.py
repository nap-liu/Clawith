"""Discover tenant models through the ordinary builtin-tool boundary."""

import json

from jsonschema import ValidationError, validate
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.services.llm.reasoning import capability_metadata
from app.services.media_ai_io import MediaAIError
from app.services.media_ai_tools import error_result, media_tool_enabled
from app.services.model_capabilities import model_modalities, model_purposes
from app.services.model_platform import model_service_platform


MODEL_CATALOG_SCHEMA = {
    "type": "object", "properties": {
        "query": {"type": "string", "description": "Case-insensitive model name or display-name search."},
        "purpose": {"type": "string", "enum": ["conversation", "media_understanding", "image_generation", "audio_generation", "video_generation", "speech_recognition"]},
        "input_modalities": {"type": "array", "items": {"type": "string", "enum": ["text", "image", "audio", "video"]}, "description": "Match models supporting all listed input types."},
        "provider": {"type": "string", "description": "Filter by provider, for example bailian, hunyuan or volcengine."},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "default": 50},
    }, "additionalProperties": False,
}
MODEL_CATALOG_SEED = {
    "name": "list_models", "display_name": "List Models",
    "description": "Find available models by name, purpose, input type or provider. Returns model IDs, capabilities and default parameters. Use a returned ID as model_id in read_media or generate_media.",
    "parameters_schema": MODEL_CATALOG_SCHEMA,
    "category": "media", "icon": "🔎", "is_default": True, "config": {}, "config_schema": {},
}
MODEL_CATALOG_FUNCTION = {"type": "function", "function": {
    "name": MODEL_CATALOG_SEED["name"], "description": MODEL_CATALOG_SEED["description"],
    "parameters": MODEL_CATALOG_SCHEMA,
}}


async def execute_list_models(state) -> str:
    try:
        validate(state.arguments, MODEL_CATALOG_SCHEMA)
        if not await media_tool_enabled(state.agent_id, "list_models"):
            raise MediaAIError("toolDisabled")
        async with async_session() as db:
            agent = await db.get(Agent, state.agent_id)
            if agent is None or agent.tenant_id is None:
                raise MediaAIError("contextRequired")
            rows = (await db.scalars(select(LLMModel).where(
                LLMModel.tenant_id == agent.tenant_id, LLMModel.enabled.is_(True),
            ).order_by(LLMModel.model, LLMModel.id))).all()
            args = state.arguments
            query = args.get("query", "").strip().casefold()
            provider = args.get("provider", "").strip().casefold()
            required = set(args.get("input_modalities", []))
            models = []
            for model in rows:
                platform = model_service_platform(model.provider, model.base_url)
                if query and query not in f"{model.model} {model.label}".casefold():
                    continue
                if provider and provider not in {platform.casefold(), model.provider.casefold()}:
                    continue
                if args.get("purpose") and args["purpose"] not in model_purposes(model):
                    continue
                if not required.issubset(model_modalities(model)):
                    continue
                models.append({
                    "id": str(model.id), "model": model.model, "label": model.label,
                    "provider": model.provider, "service_platform": platform,
                    "purposes": model_purposes(model), "input_modalities": model_modalities(model),
                    "context_window": model.context_window,
                    "defaults": {"temperature": model.temperature, "reasoning_effort": model.reasoning_effort,
                                 "max_output_tokens": model.max_output_tokens},
                    **capability_metadata(provider=model.provider, model=model.model, base_url=model.base_url),
                })
        offset, limit = args.get("offset", 0), args.get("limit", 50)
        page = models[offset:offset + limit]
        return json.dumps({"models": page, "total": len(models),
                           "next_offset": offset + len(page) if offset + len(page) < len(models) else None}, ensure_ascii=False)
    except ValidationError:
        return error_result(MediaAIError("invalidArguments"))
    except MediaAIError as exc:
        return error_result(exc)
