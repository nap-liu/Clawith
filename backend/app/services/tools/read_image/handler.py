"""Orchestration for read_image.

Invoked from agent_tools.execute_tool when tool_name == 'read_image'.
Returns a plain-text string with per-image blocks separated by
"--- Image N: <ref> ---".
"""

from __future__ import annotations

import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.llm import LLMModel  # module is `llm.py`, not `llm_model.py`
from app.models.tool import AgentTool, Tool
from app.services.tools.read_image.input_loader import (
    LoadError,
    LoadedImage,
    merge_config,
    load,
)
from app.services.tools.read_image.prompt import (
    OUTPUT_BLOCK_HEADER_FMT,
    SYSTEM_PROMPT,
    USER_PROMPT_HEADER,
)


# ─── Small helpers — isolated for mock-ability ──────────────────────────────

async def _load_config(
    db: AsyncSession, agent_id: uuid.UUID
) -> tuple[dict | None, dict | None]:
    """Return (tool_config, agent_override_config). Either may be None."""
    tool_row = (
        await db.execute(select(Tool).where(Tool.name == "read_image"))
    ).scalar_one_or_none()
    if tool_row is None or not tool_row.enabled:
        return None, None
    at_row = (
        await db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_row.id
            )
        )
    ).scalar_one_or_none()
    agent_cfg = at_row.config if (at_row and at_row.enabled) else None
    return tool_row.config or {}, agent_cfg


async def get_effective_read_image_max_bytes(agent_id: uuid.UUID) -> int:
    """Return the same per-file limit used by the standard image loader."""

    async with async_session() as db:
        tool_cfg, agent_cfg = await _load_config(db, agent_id)
    effective = merge_config(tool_cfg or {}, agent_cfg)
    return int(effective.get("max_image_bytes_per_file", 5 * 1024 * 1024))


async def _load_vision_model(
    db: AsyncSession, model_id: str | uuid.UUID | None
) -> LLMModel | None:
    if not model_id:
        return None
    try:
        normalized_id = (
            model_id if isinstance(model_id, uuid.UUID) else uuid.UUID(str(model_id))
        )
    except (TypeError, ValueError):
        return None
    return (
        await db.execute(
            select(LLMModel).where(LLMModel.id == normalized_id)
        )
    ).scalar_one_or_none()


async def _get_workspace(agent_id: uuid.UUID) -> Path:
    from app.services.agent_runtime_workspace import current_agent_runtime_workspace

    return current_agent_runtime_workspace(agent_id).local_root


# ─── Main entry ────────────────────────────────────────────────────────────

async def handle_read_image(
    agent_id: uuid.UUID,
    arguments: dict,
    *,
    workspace_root: Path | None = None,
) -> str:
    # Recursion defense: when read_image's own call_llm invocation uses
    # agent_id=None, the vision model falls back to the global AGENT_TOOLS
    # list, which includes read_image itself. A nested dispatch would land
    # here with agent_id=None. Reject that up-front — read_image must not
    # be called from within a vision-model tool loop.
    if agent_id is None:
        return "❌ read_image: 不能在视觉模型的工具回路中递归调用"

    image_paths = arguments.get("image_paths") or []
    if not isinstance(image_paths, list):
        return "❌ read_image: image_paths 必须是字符串数组"

    async with async_session() as db:
        tool_cfg, agent_cfg = await _load_config(db, agent_id)
        if tool_cfg is None:
            return "❌ read_image: 工具未启用或未配置"
        effective = merge_config(tool_cfg, agent_cfg)
        primary_model = await _load_vision_model(db, effective.get("model_id"))
        fallback_id = effective.get("fallback_model_id")
        fallback_model = await _load_vision_model(db, fallback_id) if fallback_id else None

    if primary_model is None and fallback_model is None:
        return "❌ read_image 未配置视觉模型，请联系管理员"
    if primary_model is not None and not getattr(primary_model, "supports_vision", False):
        primary_model = None
    if fallback_model is not None and not getattr(fallback_model, "supports_vision", False):
        fallback_model = None
    if primary_model is None and fallback_model is None:
        return "❌ read_image: 配置的模型不支持视觉"

    workspace = workspace_root or await _get_workspace(agent_id)
    load_result = await load(image_paths, workspace, effective)
    if load_result.short_circuit is not None:
        return f"❌ read_image: {load_result.short_circuit.reason}"

    # Partition successes from inline errors; build vision content array for successes
    successes: list[tuple[int, LoadedImage]] = []
    for idx, item in enumerate(load_result.items):
        if isinstance(item, LoadedImage):
            successes.append((idx, item))

    if not successes:
        return _assemble_output(load_result.items, successes_text=None)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": USER_PROMPT_HEADER},
                *[
                    {"type": "image_url", "image_url": {"url": item.data_url}}
                    for _, item in successes
                ],
            ],
        },
    ]

    from app.services.llm.caller import call_llm, is_retryable_error

    try:
        active_model = primary_model or fallback_model
        retry_model = fallback_model if primary_model is not None else None
        text = await call_llm(
            model=active_model,
            messages=messages,
            agent_name="read_image",
            role_description="vision OCR",
            # agent_id=None skips per-agent token accounting. Tools are also
            # disabled for this focused vision/OCR call.
            agent_id=None,
            skip_tools=True,
        )
        if retry_model is not None and is_retryable_error(text):
            logger.info(f"[read_image] retrying with fallback model: {retry_model.model}")
            text = await call_llm(
                model=retry_model,
                messages=messages,
                agent_name="read_image",
                role_description="vision OCR",
                agent_id=None,
                skip_tools=True,
            )
    except Exception as e:
        logger.warning(f"[read_image] upstream call_llm failed: {e}")
        return f"❌ read_image: 视觉模型调用失败 - {type(e).__name__}: {e}"

    return _assemble_output(load_result.items, successes_text=text)


def _assemble_output(
    items: list[LoadedImage | LoadError],
    successes_text: str | None,
) -> str:
    """Assemble the final tool output.

    Successful images are combined into a SINGLE block labeled with the
    display indices + refs of all included images. The LLM's response is
    placed once under that block, not duplicated. Failed images (category B)
    each produce their own inline ❌ block, preserving input order.
    """
    success_entries = [
        (i + 1, item) for i, item in enumerate(items) if isinstance(item, LoadedImage)
    ]
    failure_entries = [
        (i + 1, item) for i, item in enumerate(items) if isinstance(item, LoadError)
    ]

    blocks: list[str] = []
    if success_entries:
        if len(success_entries) == 1:
            idx, item = success_entries[0]
            header = OUTPUT_BLOCK_HEADER_FMT.format(index=idx, ref=item.display_ref)
        else:
            indices = ", ".join(str(i) for i, _ in success_entries)
            refs = ", ".join(item.display_ref for _, item in success_entries)
            header = f"--- Images {indices}: {refs} ---"
        blocks.append(header + "\n" + (successes_text or "(no response from model)"))

    for idx, item in failure_entries:
        header = OUTPUT_BLOCK_HEADER_FMT.format(index=idx, ref=item.display_ref)
        blocks.append(header + "\n❌ 读取失败: " + item.reason)

    return "\n\n".join(blocks) if blocks else "❌ read_image: 无可返回结果"
