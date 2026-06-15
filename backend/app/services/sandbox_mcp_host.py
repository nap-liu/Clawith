"""SandboxMcpHost — atomic registrar for stdio MCP entries in the aio-sandbox hub.

Responsibilities
----------------
Given a rendered stdio config ``{command, args, env}`` plus a scope
``(server_name, agent_id)``, this module:

1. Computes a **deterministic per-agent entry name** ``{server}__{sha256[:12]}``.
2. Merges the entry into ``/opt/gem/mcp-hub.json`` inside the sandbox using an
   **atomic shell command** (``flock`` + ``jq``).  The entry JSON is injected via
   ``base64`` so raw secrets never appear as plaintext in the shell command line
   (they ride inside the base64 blob).
3. Returns the entry name so callers can address it via ``SandboxMcpHubClient``.

Because the patched sandbox image changes ``MCPClient._servers`` from a
``@cached_property`` to a plain ``@property`` (reads the JSON on every call),
writes take effect immediately — **no restart or reload call is needed**.

Shell writes go through a fixed admin shell session (``ADMIN_SESSION``), kept
separate from per-agent conversation sessions managed by ``AioSandboxBackend``.

Usage::

    from app.services.sandbox_mcp_host import SandboxMcpHost, entry_name
    from app.config import settings

    host = SandboxMcpHost(settings.SANDBOX_API_URL, settings.SANDBOX_API_KEY)
    name = await host.ensure_registered(
        server_name="yunxiao",
        agent_id=str(agent.id),
        cfg={"command": "npx", "args": ["-y", "alibabacloud-devops-mcp-server"], "env": {"TOKEN": "..."}},
    )
    # name → "yunxiao__<12-char hex>"
"""
import base64
import hashlib
import json
import re

import httpx

# Hub config path inside the sandbox container.
_HUB_JSON = "/opt/gem/mcp-hub.json"
# Flock file — prevents concurrent jq writers from corrupting the JSON.
_LOCK_FILE = "/tmp/mcp-hub.lock"
# Stable admin session ID; distinct from per-agent conversation sessions.
ADMIN_SESSION = "clawith-mcp-admin"

# Defense-in-depth: only allow characters safe for hub JSON keys and shell paths.
_SAFE_SERVER_NAME = re.compile(r"^[a-z0-9_-]+$")


def entry_name(server_name: str, agent_id: str, cfg: dict) -> str:
    """Compute a deterministic per-agent hub entry name.

    The name is ``{server_name}__{sha256[:12]}`` where the fingerprint covers
    ``server_name``, ``agent_id``, and the sorted-key JSON of ``cfg``.

    Properties guaranteed by construction:
    - **Deterministic**: same inputs always produce the same name.
    - **Per-agent**: changing ``agent_id`` (or ``cfg``) changes the name.
    - **Safe for hub JSON keys**: only ``[a-z0-9_-]`` characters.

    Raises ``ValueError`` if *server_name* contains characters outside
    ``[a-z0-9_-]`` (defense-in-depth against shell injection).
    """
    if not _SAFE_SERVER_NAME.match(server_name):
        raise ValueError(
            f"server_name {server_name!r} contains unsafe characters; only [a-z0-9_-] allowed"
        )
    raw = f"{server_name}|{agent_id}|{json.dumps(cfg, sort_keys=True)}"
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{server_name}__{fingerprint}"


class SandboxMcpHost:
    """Registrar that merges stdio MCP entries into the sandbox hub JSON."""

    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base = (base_url or "").rstrip("/")
        self.api_key = api_key

    # ------------------------------------------------------------------ Internals

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _exec_admin_shell(self, command: str) -> dict:
        """Execute *command* in the fixed admin shell session inside the sandbox.

        Creates the session if it doesn't exist yet (idempotent — the sandbox
        returns success even if the session already exists).

        Raises ``Exception`` if either HTTP request fails or returns a non-200
        status code.
        """
        try:
            async with httpx.AsyncClient() as c:
                # Ensure admin session exists (idempotent).
                create_resp = await c.post(
                    f"{self.base}/v1/shell/sessions/create",
                    json={"id": ADMIN_SESSION, "exec_dir": "/tmp"},
                    headers=self._headers(),
                    timeout=10.0,
                )
                if create_resp.status_code != 200:
                    raise Exception(
                        f"admin shell session create HTTP {create_resp.status_code}: "
                        f"{create_resp.text[:200]}"
                    )

                exec_resp = await c.post(
                    f"{self.base}/v1/shell/exec",
                    json={"id": ADMIN_SESSION, "command": command, "timeout": 30.0},
                    headers=self._headers(),
                    timeout=40.0,
                )
                if exec_resp.status_code != 200:
                    raise Exception(
                        f"admin shell exec HTTP {exec_resp.status_code}: "
                        f"{exec_resp.text[:200]}"
                    )
                return exec_resp.json()
        except httpx.HTTPError as e:
            raise Exception(f"admin shell HTTP error: {e}") from e

    # ------------------------------------------------------------------ Public API

    async def ensure_registered(self, server_name: str, agent_id: str, cfg: dict) -> str:
        """Merge a stdio MCP entry into the sandbox hub config and return its name.

        The entry JSON is base64-encoded before being embedded in the shell
        command so that secrets in ``cfg["env"]`` never appear as plaintext
        in the command string.

        The merge is atomic: ``flock`` serialises concurrent writers and ``jq``
        updates only the single key without touching other entries.

        Idempotent: calling again with the same inputs overwrites the same key
        with the same value (no-op from the hub's perspective).

        Raises ``ValueError`` if *server_name* contains unsafe characters or if
        *cfg* does not contain a non-empty ``"command"`` key.
        """
        if not cfg.get("command"):
            raise ValueError("cfg must contain a non-empty 'command' key")

        name = entry_name(server_name, agent_id, cfg)
        entry = {
            "type": "stdio",
            "command": cfg["command"],
            "args": cfg.get("args", []),
            "env": cfg.get("env", {}),
        }

        # Encode entry JSON as base64 — secrets stay inside the blob, never
        # visible as plaintext in the shell command string (or in process lists).
        b64 = base64.b64encode(json.dumps(entry).encode()).decode()

        # Shell command breakdown:
        # 1. Ensure the directory exists and the hub JSON is initialised.
        # 2. Decode base64 entry to a temp file (so jq can --slurpfile it).
        # 3. flock + jq: atomically update .mcpServers[name] = entry.
        # 4. Move tmp file over original (atomic rename).
        tmp_entry = f"/tmp/{name}.json"
        merge_cmd = (
            f"mkdir -p $(dirname {_HUB_JSON}); "
            f"[ -f {_HUB_JSON} ] || echo '{{\"mcpServers\":{{}}}}' > {_HUB_JSON}; "
            f"echo {b64} | base64 -d > {tmp_entry}; "
            f"flock {_LOCK_FILE} -c "
            f"'jq --arg n \"{name}\" --slurpfile e {tmp_entry} "
            f"\".mcpServers[\\$n] = \\$e[0]\" {_HUB_JSON} > {_HUB_JSON}.tmp "
            f"&& mv {_HUB_JSON}.tmp {_HUB_JSON}'"
        )

        res = await self._exec_admin_shell(merge_cmd)
        if not res.get("success"):
            raise Exception(f"hub register failed: {res}")

        # No restart or reload required — the patched sandbox image reads
        # mcp-hub.json on every request (@property instead of @cached_property).
        return name

    async def deregister(self, name: str) -> None:
        """Remove a stdio MCP entry from the sandbox hub config by its entry name.

        The entry name must match the same ``[a-z0-9_-]`` pattern enforced
        by :func:`entry_name` (the combined ``{server}__{hash}`` form is safe
        under that same rule since ``__`` is two underscores, each in ``[a-z0-9_-]``).

        Raises ``ValueError`` if *name* contains unsafe characters.
        The delete is atomic: ``flock`` + ``jq del(.mcpServers[$n])``.
        """
        # Validate the full entry name with the same safe-char rule.
        if not _SAFE_SERVER_NAME.match(name):
            raise ValueError(
                f"entry name {name!r} contains unsafe characters; only [a-z0-9_-] allowed"
            )

        del_cmd = (
            f"flock {_LOCK_FILE} -c "
            f"'jq --arg n \"{name}\" \"del(.mcpServers[\\$n])\" "
            f"{_HUB_JSON} > {_HUB_JSON}.tmp "
            f"&& mv {_HUB_JSON}.tmp {_HUB_JSON}'"
        )
        await self._exec_admin_shell(del_cmd)
