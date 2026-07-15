import base64
import json
import re
from unittest.mock import patch

import httpx

from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import (
    AioSandboxBackend,
    compute_session_anchor,
    compute_session_namespace,
)
from app.services.tool_seeder import BUILTIN_TOOLS


def _backend() -> AioSandboxBackend:
    return AioSandboxBackend(
        SandboxConfig(
            type=SandboxType.AIO_SANDBOX,
            api_url="http://sandbox.test",
            default_timeout=30,
            max_timeout=300,
        )
    )


def _decode_transport(command: str) -> str:
    match = re.search(r"source <\(echo ([A-Za-z0-9+/=]+) \| base64 -d\)", command)
    assert match, command
    return base64.b64decode(match.group(1)).decode()


def _wrapper_payloads(script: str) -> list[str]:
    return [
        base64.b64decode(value).decode()
        for value in re.findall(r"echo ([A-Za-z0-9+/=]+) \| base64 -d >", script)
    ]


async def test_background_job_reuses_environment_and_permission_composer():
    requests: list[tuple[str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/sessions/create"):
            return httpx.Response(200, json={"success": True, "data": {}})
        if request.url.path.endswith("/exec"):
            return httpx.Response(
                200,
                json={"success": True, "data": {"status": "running"}},
            )
        if request.url.path.endswith("/view"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "running",
                        "output": "authorization-code-ready",
                        "exit_code": None,
                    },
                },
            )
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    inject = {
        "wrappers": [
            {
                "name": "svc",
                "binary_path": "/data/cli/svc",
                "env": {"YYBPC_CLI_USER_PHONE": "13800000000"},
            }
        ]
    }
    with patch(
        "app.services.sandbox.remote.aio_sandbox_backend.httpx.AsyncClient",
        return_value=client,
    ):
        result = await _backend().start_background_job(
            code="svc auth wait",
            language="bash",
            timeout=900,
            work_dir="/data/agents/A",
            agent_id="A",
            conversation_id="C",
            inject=inject,
        )

    assert result["success"] is True
    assert result["status"] == "running"
    assert result["output"] == "authorization-code-ready"
    assert re.fullmatch(r"job_[0-9a-f]{12}", result["job_id"])

    exec_body = next(body for method, path, body in requests if path.endswith("/exec"))
    assert exec_body["async_mode"] is True
    assert exec_body["timeout"] == 900.0
    script = _decode_transport(exec_body["command"])
    namespace = compute_session_namespace(compute_session_anchor("A", "C"))
    expected_bindir = f'$HOME/.jobs/{namespace}/{result["job_id"]}/bin'
    expected_log = f'$HOME/.jobs/{namespace}/{result["job_id"]}/output.log'
    assert "cd '/data/agents/A'" in script
    assert "export HOME='/data/agents/A'" in script
    assert "export PIP_USER=1" in script
    assert 'export NPM_CONFIG_PREFIX="$HOME/.npm-global"' in script
    assert "export CI=true" in script
    assert "export GIT_TERMINAL_PROMPT=0" in script
    assert f'export PATH="{expected_bindir}:$PATH"' in script
    assert f'tee -a "{expected_log}"' in script
    wrappers = _wrapper_payloads(script)
    assert len(wrappers) == 1
    assert "YYBPC_CLI_USER_PHONE='13800000000'" in wrappers[0]
    assert "13800000000" not in script
    assert ".clawith-jobs" not in script


async def test_job_list_is_filtered_to_current_chat_session():
    anchor = compute_session_anchor("A", "C")
    namespace = compute_session_namespace(anchor)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "sessions": {
                        f"aio-job-{namespace}-job_111111111111": {
                            "status": "running",
                            "created_at": "2026-07-15T00:00:00Z",
                            "age_seconds": 3,
                        },
                        "aio-job-other-job_222222222222": {
                            "status": "running"
                        },
                        "clawith-A:C": {"status": "completed"},
                    }
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch(
        "app.services.sandbox.remote.aio_sandbox_backend.httpx.AsyncClient",
        return_value=client,
    ):
        result = await _backend().manage_background_jobs(
            action="list_jobs",
            agent_id="A",
            conversation_id="C",
        )

    assert result == {
        "success": True,
        "status": "ok",
        "jobs": [
            {
                "job_id": "job_111111111111",
                "status": "running",
                "created_at": "2026-07-15T00:00:00Z",
                "age_seconds": 3,
            }
        ],
    }


async def test_job_stop_deletes_only_the_requested_job_session():
    deleted: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/view"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "running",
                        "output": "still alive",
                        "exit_code": None,
                    },
                },
            )
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200, json={"success": True})
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch(
        "app.services.sandbox.remote.aio_sandbox_backend.httpx.AsyncClient",
        return_value=client,
    ):
        result = await _backend().manage_background_jobs(
            action="job_stop",
            agent_id="A",
            conversation_id="C",
            job_id="job_111111111111",
        )

    namespace = compute_session_namespace(compute_session_anchor("A", "C"))
    assert result["status"] == "stopped"
    assert deleted == [
        f"/v1/shell/sessions/aio-job-{namespace}-job_111111111111"
    ]


async def test_running_job_logs_are_read_from_shared_agent_home(tmp_path):
    anchor = compute_session_anchor("A", "C")
    namespace = compute_session_namespace(anchor)
    log_dir = tmp_path / ".jobs" / namespace / "job_111111111111"
    log_dir.mkdir(parents=True)
    (log_dir / "output.log").write_text("line-1\nline-2\nline-3\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/view")
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "status": "running",
                    "output": "",
                    "exit_code": None,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch(
        "app.services.sandbox.remote.aio_sandbox_backend.httpx.AsyncClient",
        return_value=client,
    ):
        result = await _backend().manage_background_jobs(
            action="job_logs",
            agent_id="A",
            conversation_id="C",
            job_id="job_111111111111",
            tail_lines=2,
            work_dir=str(tmp_path),
        )

    assert result["status"] == "running"
    assert result["output"] == "line-2\nline-3"


def test_background_python_is_a_managed_shell_process():
    command = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/A",
        code="print('hello')",
        language="python",
        bindir="$HOME/.jobs/ns/job_x/bin",
        background_job=True,
    )
    script = _decode_transport(command)
    assert "python -u <<'AIOSB_PYTHON_" in script
    assert "print('hello')" in script


def test_seeded_tool_exposes_one_simple_execute_and_job_management_contract():
    tool = next(item for item in BUILTIN_TOOLS if item["name"] == "execute_code_aio")
    schema = tool["parameters_schema"]
    actions = schema["properties"]["action"]["enum"]

    assert actions == [
        "execute",
        "list_jobs",
        "job_status",
        "job_logs",
        "job_stop",
    ]
    assert schema["properties"]["execution_mode"]["enum"] == [
        "foreground",
        "background",
    ]
    assert "required" not in schema
    assert ".clawith-jobs" not in tool["description"]
    assert tool["config"]["background_default_timeout"] < tool["config"][
        "background_max_timeout"
    ]
