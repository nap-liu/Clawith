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
from app.config import get_settings
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


async def _load_vision_model(
    db: AsyncSession, model_id: str | uuid.UUID | None
) -> LLMModel | None:
    if model_id is None:
        return None
    return (
        await db.execute(
            select(LLMModel).where(
                LLMModel.id == (uuid.UUID(str(model_id)) if not isinstance(model_id, uuid.UUID) else model_id)
            )
        )
    ).scalar_one_or_none()


async def _get_workspace(agent_id: uuid.UUID) -> Path:
    settings = get_settings()
    return Path(settings.AGENT_DATA_DIR) / str(agent_id)


# ─── Main entry ────────────────────────────────────────────────────────────

async def handle_read_image(agent_id: uuid.UUID, arguments: dict) -> str:
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
        model = await _load_vision_model(db, effective.get("model_id"))

    if model is None:
        return "❌ read_image 未配置视觉模型，请联系管理员"
    if not getattr(model, "supports_vision", False):
        return "❌ read_image: 配置的模型不支持视觉"

    workspace = await _get_workspace(agent_id)
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

    from app.services.llm.caller import call_llm

    try:
        text = await call_llm(
            model=model,
            messages=messages,
            agent_name="read_image",
            role_description="vision OCR",
            # agent_id=None here skips per-agent token accounting. Recursion
            # into read_image is blocked by the agent_id=None guard at
            # handle_read_image's entry, so the vision model cannot call
            # itself through AGENT_TOOLS even though it sees the catalogue.
            agent_id=None,
            supports_vision=True,
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
