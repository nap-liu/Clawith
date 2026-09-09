"""Provider-neutral model purposes and input capabilities."""

from typing import Literal

from sqlalchemy import String, cast, or_

APIProtocol = Literal["openai_compatible", "openai_responses", "anthropic", "gemini"]
ModelPurpose = Literal["conversation", "media_understanding", "image_generation", "audio_generation", "video_generation", "speech_recognition"]
InputModality = Literal["text", "image", "audio", "video"]
MEDIA_MODEL_DEFAULTS = {
    "understanding_model_id": "media_understanding",
    "image_model_id": "image_generation",
    "audio_model_id": "audio_generation",
    "video_model_id": "video_generation",
    "speech_model_id": "speech_recognition",
}


def model_purposes(model) -> list[str]:
    value = getattr(model, "purposes", None)
    return list(value) if isinstance(value, (list, tuple)) else ["conversation"]


def model_modalities(model) -> list[str]:
    value = getattr(model, "input_modalities", None)
    result = list(value) if isinstance(value, (list, tuple)) else ["text"]
    if getattr(model, "supports_vision", False) is True and "image" not in result:
        result.append("image")
    return result


def supports_purpose(model, purpose: str = "conversation") -> bool:
    return purpose in model_purposes(model)


def purpose_clause(purpose: str = "conversation"):
    from app.models.llm import LLMModel

    # Enumerated string values are matched as complete JSON strings. This also
    # supports the repository's SQLite component tests without another adapter.
    clause = cast(LLMModel.purposes, String).contains(f'"{purpose}"')
    return or_(LLMModel.purposes.is_(None), clause) if purpose == "conversation" else clause
