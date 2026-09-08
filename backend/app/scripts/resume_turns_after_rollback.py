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
from app.services.redis_lease_lock import RedisLeaseLock
from app.services.turn_recovery import (
    STARTUP_RECOVERY_LEASE_RESOURCE,
    _load_recoverable_anchors,
    _resume_one,
)


async def snapshot(path: Path) -> dict:
    async with async_session() as db:
        anchors = await _load_recoverable_anchors(db)
        identities = [
            {"anchor_id": str(anchor.id), "generation": anchor.message_meta.get("turn_generation", 0)}
            for anchor in anchors
        ]
    path.write_text(json.dumps(identities), encoding="utf-8")
    return {"mode": "snapshot", "anchors": len(identities)}


async def apply(path: Path) -> dict:
    identities = json.loads(path.read_text(encoding="utf-8"))
    targets = {uuid.UUID(item["anchor_id"]): int(item["generation"]) for item in identities}
    report = {"mode": "apply", "resumed": 0, "failed": 0, "skipped": 0, "already_finished": 0, "superseded": 0}
    async with RedisLeaseLock(STARTUP_RECOVERY_LEASE_RESOURCE, namespace="turn-recovery"):
        anchors = []
        async with async_session() as db:
            for anchor_id, generation in targets.items():
                anchor = await db.get(ChatMessage, anchor_id)
                session = await db.get(ChatSession, uuid.UUID(anchor.conversation_id)) if anchor else None
                state = conversation_turn_snapshot_for_session(session) if session else None
                if state is None or state.anchor_id != anchor_id or state.generation != generation:
                    report["superseded"] += 1
                elif state.status in {"completed", "cancelled", "failed"}:
                    report["already_finished"] += 1
                else:
                    anchors.append(anchor)
        results = await asyncio.gather(*(_resume_one(anchor) for anchor in anchors))
        for result in results:
            report["resumed"] += result.resumed
            report["failed"] += result.failed
            report["skipped"] += result.skipped
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
    return 1 if result.get("failed") or result.get("skipped") else 0


if __name__ == "__main__":
    raise SystemExit(main())
