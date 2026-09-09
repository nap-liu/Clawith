"""Local Docker fault drill: a stable HTTP provider plus durable Task evidence.

Run only against an isolated test database. The provider stays alive while real
application worker containers are killed or stopped; it is never a model mock
inside the worker. Container lifecycle is controlled by the drill operator.
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.database import async_session, engine
import app.models.registry  # noqa: F401
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User

EVIDENCE = Path(os.environ.get("TURN_DRILL_EVIDENCE", "/evidence"))
CASES = ("repeated-kill", "parallel-fast")


def require_test_database():
    name = make_url(os.environ["DATABASE_URL"]).database or ""
    if not name.startswith("test_"):
        raise RuntimeError("Fault drills require an isolated test_ database")


async def seed():
    from app.services.task_executor import prepare_task_turn
    from app.services.tool_seeder import seed_builtin_tools
    from app.services.agent_runtime_workspace import standard_agent_runtime_workspace

    await seed_builtin_tools()
    references = {}
    for case in CASES:
        async with async_session() as db:
            suffix = uuid.uuid4().hex[:10]
            tenant = Tenant(name="Recovery drill", slug=f"recovery-drill-{suffix}")
            identity = Identity(username=f"recovery-{suffix}", email=f"{suffix}@example.com")
            db.add_all([tenant, identity])
            await db.flush()
            user = User(tenant_id=tenant.id, identity_id=identity.id, display_name="Drill operator",
                        is_active=True, role="member")
            db.add(user)
            await db.flush()
            model = LLMModel(tenant_id=tenant.id, provider="openai", model=case,
                api_key_encrypted="local-fixture", base_url="http://turn-fault-provider:9000/v1",
                label="Local fault fixture", request_timeout=300,
                context_window=100000, max_output_tokens=1000)
            db.add(model)
            await db.flush()
            agent = Agent(tenant_id=tenant.id, creator_id=user.id, name=case,
                          status="running", primary_model_id=model.id, access_mode="company",
                          heartbeat_enabled=False)
            db.add(agent)
            await db.flush()
            tool = await db.scalar(select(Tool).where(Tool.name == "read_file"))
            db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
            task = Task(agent_id=agent.id, title=case, description="Read the report and finish",
                        created_by=user.id, execution_user_id=user.id, soul=False, memory=False,
                        status="pending")
            db.add(task)
            await db.commit()
            anchor = await prepare_task_turn(db, task.id, agent.id, user.id)
            await db.commit()
            references[case] = {"task_id": str(task.id), "anchor_id": str(anchor.id),
                                "session_id": anchor.conversation_id, "agent_id": str(agent.id)}
            path = standard_agent_runtime_workspace(agent.id).local_path("workspace/report.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Durable report for container restart drill", encoding="utf-8")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "cases.json").write_text(json.dumps(references, indent=2))
    print(json.dumps({"accepted_before_dispatch": references}))


async def serve():
    requests = {case: [] for case in CASES}
    gate = asyncio.Event()

    async def handle(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            first, *headers = header.split(b"\r\n")
            _, path, _ = first.decode().split(" ", 2)
            if path == "/state":
                payload = {case: {"requests": len(rows),
                    "tool_histories": [[m.get("content", "") for m in row["messages"]
                                        if m.get("role") == "tool"] for row in rows]}
                           for case, rows in requests.items()}
            elif path == "/release":
                gate.set()
                payload = {"released": True}
            else:
                length = next(int(line.split(b":", 1)[1]) for line in headers
                              if line.lower().startswith(b"content-length:"))
                request = json.loads(await reader.readexactly(length))
                case = request["model"]
                requests[case].append(request)
                EVIDENCE.mkdir(parents=True, exist_ok=True)
                (EVIDENCE / "provider-requests.json").write_text(json.dumps(requests, indent=2))
                if case == "repeated-kill" and not any(m["role"] == "tool" for m in request["messages"]):
                    response = {"tool_calls": [{"id": "container-report-read", "type": "function",
                        "function": {"name": "read_file",
                                     "arguments": json.dumps({"path": "workspace/report.txt"})}}]}
                else:
                    if case == "repeated-kill":
                        await gate.wait()
                    response = {"content": f"Completed {case}"}
                if request.get("stream"):
                    delta = dict(response)
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(delta["tool_calls"])]
                    frame = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    finish = {"choices": [{"index": 0, "delta": {},
                        "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
                    body = (f"data: {json.dumps(frame)}\n\ndata: {json.dumps(finish)}\n\ndata: [DONE]\n\n").encode()
                    await respond(writer, body, "text/event-stream")
                    return
                payload = {"choices": [{"message": {"role": "assistant", **response}, "finish_reason": "stop"}]}
            await respond(writer, json.dumps(payload).encode(), "application/json")
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    server = await asyncio.start_server(handle, "0.0.0.0", 9000)
    print("Provider ready", flush=True)
    async with server:
        await server.serve_forever()


async def respond(writer, body, content_type):
    writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                  f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
    await writer.drain()


async def inspect(final=False):
    references = json.loads((EVIDENCE / "cases.json").read_text())
    outcomes = {}
    async with async_session() as db:
        for case, refs in references.items():
            anchor = await db.get(ChatMessage, uuid.UUID(refs["anchor_id"]))
            session = await db.get(ChatSession, uuid.UUID(refs["session_id"]))
            task = await db.get(Task, uuid.UUID(refs["task_id"]))
            rows = list(await db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == refs["session_id"])))
            terminals = [row for row in rows if row.role == "assistant"
                         and (row.message_meta or {}).get("turn_status") in {"completed", "failed"}]
            tools = [json.loads(row.content) for row in rows if row.role == "tool_call"]
            logs = list(await db.scalars(select(TaskLog).where(TaskLog.task_id == task.id)))
            outcomes[case] = {"task": task.status, "turn": anchor.message_meta["turn_status"],
                "session_turn": session.im_config["conversation_turn"]["status"],
                "finals": len(terminals), "task_logs": len(logs),
                "completed_tools": sum(tool.get("status") == "done" for tool in tools),
                "delivered": anchor.message_meta["background_execution"]["delivered"]}
            if final:
                assert task.status == "done", outcomes
                assert outcomes[case]["turn"] == outcomes[case]["session_turn"] == "completed", outcomes
                assert len(terminals) == 1 and len(logs) == 2, outcomes
                assert terminals[0].content == f"Completed {case}", outcomes
                assert outcomes[case]["delivered"] is True, outcomes
                assert outcomes[case]["completed_tools"] == (1 if case == "repeated-kill" else 0), outcomes
    if final:
        requests = json.loads((EVIDENCE / "provider-requests.json").read_text())
        task_requests = {case: [request for request in rows if any(
            "Task Execution Mode" in str(message.get("content", ""))
            for message in request["messages"])] for case, rows in requests.items()}
        assert len(task_requests["parallel-fast"]) == 1
        repeated = task_requests["repeated-kill"]
        assert len(repeated) == 5, len(repeated)
        assert all(any(m["role"] == "tool" and "Durable report" in str(m.get("content"))
                       for m in request["messages"]) for request in repeated[1:])
        (EVIDENCE / "verified.json").write_text(json.dumps(outcomes, indent=2))
    print(json.dumps(outcomes, indent=2))


async def main():
    require_test_database()
    try:
        if sys.argv[1] == "serve":
            await serve()
        elif sys.argv[1] == "seed":
            await seed()
        else:
            await inspect(final=sys.argv[1] == "verify")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
