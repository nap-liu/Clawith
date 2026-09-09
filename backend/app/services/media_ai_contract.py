"""Unified media tools backed by the enterprise model pool."""

MEDIA_AI_NAMES = frozenset({"read_media", "generate_media"})
MEDIA_AI_CONFIG_KEY = "media_ai"
MEDIA_AI_DEFAULTS = {
    "base_url": "https://dashscope.aliyuncs.com",
    "understanding_model": "qwen3.5-omni-flash",
    "image_model": "qwen-image-2.0-pro",
    "audio_model": "qwen3-tts-flash",
    "video_model": "wan3.0-video",
    "context_window": 262144,
    "context_usage_ratio": 0.7,
    "max_output_tokens": 4096,
}
MEDIA_AI_CONFIG_SCHEMA = {
    "fields": [
        {"key": f"{kind}_model_id", "label": f"mediaAI.{kind}_model_id", "type": "llm_model_picker",
         "purpose": "media_understanding" if kind == "understanding" else f"{kind}_generation"}
        for kind in ("understanding", "image", "audio", "video")
    ],
}

_FILES = {
    "type": "array", "items": {"oneOf": [
        {"type": "string", "minLength": 1},
        {"type": "object", "properties": {
            "source": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": ["image", "audio", "video"]},
            "role": {"type": "string", "enum": ["first_frame", "last_frame", "reference_image", "reference_video", "reference_audio"]},
        }, "required": ["source"], "additionalProperties": False},
    ]},
    "description": "AgentDir paths, third-party HTTP(S) URLs or base64 media data URLs. Optional kind avoids probing opaque URLs; role selects a generation reference role.",
}
_SESSION = {"type": "string", "format": "uuid", "description": "Continue a media session returned by an earlier call. Omit to start one."}
_MODEL = {"type": "string", "format": "uuid", "description": "Optional enterprise model ID. Omit to use the media session selection or configured company model."}
READ_MEDIA_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {**_FILES, "minItems": 1},
        "session_id": _SESSION,
        "model_id": _MODEL,
        "prompt": {"type": "string", "minLength": 1,
                   "description": "Question or analysis request about the supplied media."},
    },
    "required": ["prompt"], "additionalProperties": False,
}
GENERATE_MEDIA_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": _SESSION,
        "model_id": _MODEL,
        "prompt": {"type": "string", "minLength": 1,
                   "description": "Complete instructions for this image/video, including any prior constraints to retain; or exact text to speak for audio."},
        "output_type": {"type": "string", "enum": ["image", "audio", "video"]},
        "files": {**_FILES, "description": "Reference media. On continuation, omitted files reuse the latest compatible result; [] starts without references."},
        "ratio": {"type": "string", "minLength": 1,
                  "description": "Optional image/video aspect ratio."},
        "duration": {"type": "integer",
                     "description": "Video duration in seconds. Omit to use the selected model's default."},
        "voice": {"type": "string", "minLength": 1, "maxLength": 80,
                   "description": "Optional provider voice for speech. Omit to use the selected model's default."},
        "resolution": {"type": "string", "description": "Provider video resolution, for example 480P or 720P."},
        "size": {"type": "string", "description": "Provider image dimensions, for example 1024*1024."},
    },
    "required": ["prompt", "output_type"], "additionalProperties": False,
}

MEDIA_AI_SEEDS = [
    {
        "name": "read_media", "display_name": "Read Media",
        "description": "Read one or multiple images, audio or videos together asynchronously, including video sound when supported by the selected enterprise model. Returns task_id and session_id immediately; completion arrives automatically. Supply session_id for follow-up questions. Do not poll or resubmit while pending. Uses the company model pool.",
        "parameters_schema": READ_MEDIA_SCHEMA,
    },
    {
        "name": "generate_media", "display_name": "Generate Media",
        "description": "Generate/edit images, synthesize speech, or generate/edit/extend video asynchronously using the selected enterprise model's capabilities. Returns task_id and session_id immediately; results arrive automatically. Supply session_id to continue with prior references. For audio, prompt is the complete text to speak. Do not poll or resubmit while pending. Uses the company model pool.",
        "parameters_schema": GENERATE_MEDIA_SCHEMA,
    },
]
MEDIA_AI_SEEDS = [
    {**seed, "category": "media", "icon": "🎬", "is_default": seed["name"] == "read_media",
     "config": {}, "config_schema": {"fields": [
         field for field in MEDIA_AI_CONFIG_SCHEMA["fields"]
         if (field["key"] == "understanding_model_id") == (seed["name"] == "read_media")
     ]}}
    for seed in MEDIA_AI_SEEDS
]
MEDIA_AI_FUNCTIONS = [
    {"type": "function", "function": {
        "name": seed["name"], "description": seed["description"],
        "parameters": seed["parameters_schema"],
    }} for seed in MEDIA_AI_SEEDS
]
