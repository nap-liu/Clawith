"""Media configuration projected into the shared immutable model contract."""

import json
import uuid
from dataclasses import replace

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.services.llm.runtime_model import RuntimeLLMModel
from app.services.model_headers import encrypt_model_headers


async def media_context_model(agent, request: dict) -> RuntimeLLMModel:
    config = dict(request["config"])
    snapshot = json.loads(decrypt_data(request["connection_ref"], get_settings().SECRET_KEY))
    if snapshot.get("runtime_model"):
        values = dict(snapshot["runtime_model"])
        values["id"] = uuid.UUID(values["id"])
        values["tenant_id"] = uuid.UUID(values["tenant_id"]) if values.get("tenant_id") else None
        return replace(RuntimeLLMModel(**values), compact_stream=True)
    return RuntimeLLMModel(
        id=uuid.uuid5(uuid.NAMESPACE_URL, config["base_url"] + "/" + config["understanding_model"]),
        tenant_id=agent.tenant_id, provider="openai", model=config["understanding_model"],
        api_key_encrypted=encrypt_data(snapshot["api_key"], get_settings().SECRET_KEY),
        extra_headers_encrypted=encrypt_model_headers(snapshot.get("extra_headers")),
        base_url=config["base_url"] + "/compatible-mode/v1", label=config["understanding_model"],
        max_tokens_per_day=None, enabled=True, supports_vision=True, temperature=None,
        request_timeout=180, max_output_tokens=int(config["max_output_tokens"]),
        context_window=int(config["context_window"]), context_usage_ratio=float(config["context_usage_ratio"]),
        compact_trigger_ratio=float(config["context_usage_ratio"]), keep_recent_turns=3,
        compact_summary_max_tokens=min(2000, int(config["max_output_tokens"])),
        compact_stream=True,
    )
