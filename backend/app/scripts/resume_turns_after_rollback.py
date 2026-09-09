"""Snapshot turn owners before rollback and resume only those exact identities.

Old application roles must start with TURN_RECOVERY_ENABLED=false. Run both
commands from the candidate image with its entrypoint overridden. The snapshot
contains audit identities only; it never changes turn state or freezes ingress.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.conversation_turn_lifecycle import conversation_turn_snapshot_for_session
from sqlalchemy import select
from app.services.redis_lease_lock import RedisLeaseLock
from app.services.turn_recovery import (
    STARTUP_RECOVERY_LEASE_RESOURCE,
    _load_recoverable_anchors,
    _resume_one,
)


def _root_id(candidate: ChatMessage) -> uuid.UUID | None:
    if candidate.role != "assistant":
        return candidate.id
    try:
        return uuid.UUID(str((candidate.message_meta or {}).get("turn_anchor_id")))
    except (TypeError, ValueError):
        return None


async def _finished(db, anchor: ChatMessage) -> bool:
    metadata = anchor.message_meta or {}
    if metadata.get("turn_status") not in {"completed", "failed", "cancelled"}:
        return False
    background = metadata.get("background_execution")
    if background:
        return bool(background.get("finalized") and background.get("delivered"))
    replies = (await db.scalars(select(ChatMessage).where(
        ChatMessage.agent_id == anchor.agent_id,
        ChatMessage.conversation_id == anchor.conversation_id,
        ChatMessage.role == "assistant",
        ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
        ChatMessage.message_meta["turn_status"].as_string().in_(("completed", "failed", "cancelled")),
    ))).all()
    if not replies:
        return metadata.get("turn_status") == "cancelled"
    return all((row.message_meta or {}).get("delivery", {}).get("status") == "sent"
               for row in replies)


async def snapshot(path: Path) -> dict:
    async with async_session() as db:
        candidates = await _load_recoverable_anchors(db, include_legacy=False)
        identities = {}
        for candidate in candidates:
            root_id = _root_id(candidate)
            anchor = await db.get(ChatMessage, root_id) if root_id else None
            if anchor is None:
                continue
            identities[str(anchor.id)] = {
                "anchor_id": str(anchor.id),
                "generation": int((anchor.message_meta or {}).get("turn_generation", 0)),
            }
    path.write_text(json.dumps(list(identities.values())), encoding="utf-8")
    return {"mode": "snapshot", "anchors": len(identities)}


async def apply(path: Path) -> dict:
    identities = json.loads(path.read_text(encoding="utf-8"))
    targets = {uuid.UUID(item["anchor_id"]): int(item["generation"]) for item in identities}
    report = {"mode": "apply", "resumed": 0, "failed": 0, "skipped": 0,
              "already_finished": 0, "superseded": 0, "pending": 0, "waiting_confirmation": 0, "business_failed": 0}
    async with RedisLeaseLock(STARTUP_RECOVERY_LEASE_RESOURCE, namespace="turn-recovery"):
        candidates = []
        unfinished = set()
        async with async_session() as db:
            for anchor_id, generation in targets.items():
                anchor = await db.get(ChatMessage, anchor_id)
                if anchor is None or int((anchor.message_meta or {}).get("turn_generation", 0)) != generation:
                    report["superseded"] += 1
                elif await _finished(db, anchor):
                    report["already_finished"] += 1
                    report["business_failed"] += int((anchor.message_meta or {}).get("turn_status") == "failed")
                else:
                    session = await db.get(ChatSession, uuid.UUID(anchor.conversation_id))
                    state = conversation_turn_snapshot_for_session(session) if session else None
                    status = (anchor.message_meta or {}).get("turn_status")
                    if status not in {"completed", "failed", "cancelled"} and (
                        state is None or state.anchor_id != anchor.id or state.generation != generation
                    ):
                        report["superseded"] += 1
                    else:
                        unfinished.add(anchor_id)
            # The shared scanner owns discoverability. A terminal reply keeps its
            # original anchor identity even if the Session already advanced.
            for candidate in await _load_recoverable_anchors(db, include_legacy=False):
                root_id = _root_id(candidate)
                if root_id not in unfinished:
                    continue
                root = await db.get(ChatMessage, root_id)
                if int((root.message_meta or {}).get("turn_generation", 0)) != targets[root_id]:
                    continue
                if (root.message_meta or {}).get("turn_status") != "suspended":
                    candidates.append(candidate)
        results = await asyncio.gather(*(_resume_one(candidate, resume_promoted_turn=False) for candidate in candidates))
        for result in results:
            report["resumed"] += result.resumed
            report["failed"] += result.failed
            report["skipped"] += result.skipped
        # A helper success is durable completion, not merely a successful scan.
        # Recheck only the frozen identities; never adopt work accepted later.
        async with async_session() as db:
            for anchor_id in unfinished:
                anchor = await db.get(ChatMessage, anchor_id)
                if anchor is None or int((anchor.message_meta or {}).get("turn_generation", 0)) != targets[anchor_id]:
                    report["superseded"] += 1
                elif await _finished(db, anchor):
                    report["business_failed"] += int((anchor.message_meta or {}).get("turn_status") == "failed")
                    continue
                elif (anchor.message_meta or {}).get("turn_status") == "suspended":
                    report["waiting_confirmation"] += 1
                else:
                    report["pending"] += 1
                    report["business_failed"] += int((anchor.message_meta or {}).get("turn_status") == "failed")
    return report


async def run(action: str, path: Path) -> dict:
    try:
        return await snapshot(path) if action == "snapshot" else await apply(path)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "apply"))
    parser.add_argument("snapshot_file", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args.action, args.snapshot_file))
    print(json.dumps(result, sort_keys=True))
    return 1 if any(result.get(key) for key in (
        "failed", "skipped", "superseded", "pending", "waiting_confirmation", "business_failed",
    )) else 0


if __name__ == "__main__":
    raise SystemExit(main())
