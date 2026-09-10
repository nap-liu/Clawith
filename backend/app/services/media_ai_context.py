"""Media projection of the shared durable history and context compactor."""

from __future__ import annotations

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.chat_history_loading import load_messages_for_session
from app.services.llm.compactor import maybe_compact
from app.services.media_ai_io import load_media, load_understanding_media, media_content
from app.services.media_ai_model import media_context_model
from app.services.read_media_compat import media_input_workspace



def _native_override(parameters: dict, key: str) -> bool:
    """Current native controls outrank inherited normalized defaults."""
    aliases = {
        "duration": {"duration", "seconds"},
        "ratio": {"ratio", "aspect_ratio", "size"},
        "size": {"size", "ratio", "aspect_ratio"},
        "resolution": {"resolution", "mode", "size"},
        "voice": {"voice", "voice_id", "timbre_weights"},
    }
    if aliases[key].intersection(parameters):
        return True
    settings = parameters.get("settings")
    if isinstance(settings, dict) and aliases[key].intersection(settings):
        return True
    voice = parameters.get("voice_setting")
    return key == "voice" and isinstance(voice, dict) and "voice_id" in voice


async def prepare_media_context(agent, child, anchor, request: dict, *, prepare_inputs=None) -> tuple[dict, list[dict]]:
    args = dict(request["arguments"])
    async with async_session() as db:
        all_inputs = (await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == str(child.id), ChatMessage.role == "user",
            ChatMessage.message_meta["media_request"].is_not(None),
        ).order_by(ChatMessage.created_at, ChatMessage.id))).all()
        earlier = []
        for row in all_inputs:
            if row.id == anchor.id:
                break
            earlier.append(row)
        earlier = [row for row in earlier if (row.message_meta or {}).get("subagent_input_state") == "done"]
        previous = earlier[-1] if earlier else None
        prior_result = None
        if previous:
            prior_result = await db.scalar(select(ChatMessage).where(
                ChatMessage.conversation_id == str(child.id), ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string().in_([str(row.id) for row in earlier]),
                ChatMessage.message_meta["media_result"]["status"].as_string() == "completed",
            ).order_by(ChatMessage.created_at.desc()).limit(1))
    previous_context = dict((prior_result.message_meta or {}).get("media_context") or {}) if prior_result else {}
    if request["tool"] == "read_media":
        sources = previous_context.get("sources", [])
        if previous_context.get("files") and not args.get("files"):
            args["files"] = [{"source": item["path"], "kind": item["kind"]} for item in previous_context["files"]]
        args["_context_sources"] = []
        for source in sources + args.get("files", []):
            if source not in args["_context_sources"]:
                args["_context_sources"].append(source)
    if request["tool"] == "generate_media":
        supplied_parameters = dict(args.get("parameters") or {})
        if ("parameters" not in args and previous_context.get("output_type") == args["output_type"]
                and previous_context.get("model_id") == request.get("config", {}).get("model_id")):
            args["parameters"] = dict(previous_context.get("native_parameters") or {})
        parameter_keys = {"image": ("ratio", "size"), "video": ("ratio", "resolution", "duration"), "audio": ("voice",)}
        for key in parameter_keys[args["output_type"]]:
            if (key not in args and not _native_override(supplied_parameters, key)
                    and key in previous_context.get("parameters", {})):
                args[key] = previous_context["parameters"][key]
        if "files" not in args:
            kind = args["output_type"]
            args["files"] = [
                {"source": item["path"], "kind": item["kind"]}
                for item in previous_context.get("files", [])
                if item["kind"] == kind and kind in {"image", "video"}
            ]
    elif "files" not in args:
        args["files"] = []
    if request["tool"] == "read_media":
        if prepare_inputs is not None:
            await prepare_inputs(args, previous_context)
        model = await media_context_model(agent, request)
        usage = (prior_result.message_meta or {}).get("media_result", {}).get("usage", {}) if prior_result else {}
        await maybe_compact(
            agent_id=agent.id, conversation_id=str(child.id), model=model,
            last_prompt_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
            current_anchor_id=anchor.id,
        )
    async with async_session() as db:
        rows = await load_messages_for_session(db, agent_id=agent.id, conversation_id=str(child.id), ctx_size=0)
    completed = {str(row.id) for row in earlier if (row.message_meta or {}).get("subagent_input_state") == "done"}
    history = []
    outputs = {str((row.message_meta or {}).get("turn_anchor_id")): row for row in rows if row.role == "assistant"}
    for row in rows:
        meta = getattr(row, "message_meta", None) or {}
        if row.role == "user" and str(row.id) in completed:
            previous_request = meta.get("media_request") or {}
            old_args = previous_request.get("arguments") or {}
            sources = old_args.get("files") or []
            if request["tool"] == "read_media":
                with media_input_workspace(agent.id, previous_request):
                    media, _ = await load_understanding_media(agent.id, sources, loader=load_media)
                history.append({"role": "user", "content": media_content(row.content, media)})
            else:
                history.append({"role": "user", "content": row.content})
            result_row = outputs.get(str(row.id))
            if result_row:
                entry = {"role": "assistant", "content": result_row.content}
                snapshot = (result_row.message_meta or {}).get("responses_snapshot")
                if snapshot:
                    entry["responses_snapshot"] = snapshot
                history.append(entry)
        elif not meta.get("media_request") and row.role in {"system", "user"} and not isinstance(row, ChatMessage):
            history.append({"role": "user", "content": row.content})
    if request["tool"] == "read_media" and not args["files"]:
        has_media = any(isinstance(item.get("content"), list) and len(item["content"]) > 1 for item in history)
        if not has_media:
            args["files"] = previous_context.get("sources", [])
    # Native generation continues from reference artifacts and explicit parameters.
    # Its prompt is this turn's complete instruction; replaying a chat transcript
    # into a short generation prompt can silently truncate the current request.
    return args, history
