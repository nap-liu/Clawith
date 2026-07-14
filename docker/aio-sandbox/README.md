# clawith-aio-sandbox patched overlay

Thin overlay image on top of `all-in-one-sandbox:1.9.3` that fixes two bugs in
`/opt/python3.12/lib/python3.12/site-packages/app/services/mcp_client.py`.

This is a **temporary overlay** pending an upstream fix in agent-infra/sandbox.

## Patches

### 1. stdio unpack bug (crash on all stdio MCP servers)

The bundled `mcp` SDK's `stdio_client` context manager yields a 2-tuple
`(read_stream, write_stream)`, but the original code unpacks 3 values:

```python
# before (crashes with stdio)
async with client_context as (read_stream, write_stream, _):
```

Fixed with `*_` which tolerates both 2- and 3-tuples (streamable-http still
yields 3 and continues to work):

```python
# after
async with client_context as (read_stream, write_stream, *_):
```

### 2. No config hot-reload (requires python server restart to add MCP servers)

`_servers` and `_filtered_servers` were `@cached_property` on the singleton
`MCPClient`, so `/opt/gem/mcp-hub.json` was read once at process startup and
never re-read. Changed to plain `@property` so the small JSON file is re-read
on each access, enabling hot-reload without a server restart.

### 3. mcp-hub.json not writable by the API user (`run.sh`)

The sandbox HTTP API (`/v1/shell/*`, `/v1/file/*`) runs as the unprivileged
`gem` user, but `run.sh` (root) generates `/opt/gem/mcp-hub.json` root-owned in
a root-only directory. Clawith registers stdio MCP servers at runtime by
overwriting that file **in place** through the API (`cat > file`, never
`.tmp`+`mv` — `gem` cannot create files in the root-owned dir). So `run.sh` is
patched to `chmod 666 /opt/gem/mcp-hub.json` right after generating it (the only
change vs the upstream run.sh). Without this, runtime registration fails with
`Permission denied`.

### 4. Real tool errors masked as a generic 500 (`mcp.py`)

`app/api/v1/mcp.py`'s tool-call endpoint caught any exception from the MCP
server and returned a generic `Failed to execute tool '<tool>' on MCP server
'<server>'` with HTTP 500, while only *logging* the real error. So when an
stdio MCP server returned a JSON-RPC error carrying the upstream API message
(e.g. `Yunxiao API error (400): invalid workitemTypeId` / missing required
field), that message never reached Clawith or the LLM — the agent flew blind and
kept retrying with guessed arguments. Patched to append `: {e}` to the 500
`detail` so the real reason is surfaced (Clawith already forwards the 500 body to
the LLM, which can then self-correct). The only change vs upstream is that one
f-string.

### 5. `/v1/shell` timeout enforcement and session cleanup (`shell.py`, `bash.py`)

The bundled OpenHands `BashSession` reported `hard_timeout` without signalling
the command. The foreground process kept running, the next command hit the same
busy tmux pane, and deleting the session only killed tmux while signal-resistant
children survived as PID 1 orphans.

The patched `/v1/shell` keeps the existing single `timeout` request field and
treats it as the actual command deadline instead of merely an HTTP polling
window. The overlay uses Linux terminal job-control metadata (`tpgid`) to terminate
only the current foreground process group with `SIGINT` → `SIGTERM` → `SIGKILL`,
waits until bash regains the pane, and then returns `hard_timeout`. Existing
shell state and previously launched background jobs are preserved. Explicit
session close remains destructive by design and additionally reaps every
process still attached to the pane shell's Linux process session.

The Dockerfile verifies the upstream `bash.py` SHA256 before copying the patch,
so an upstream image change cannot silently apply this overlay to incompatible
code.

`MAX_SHELL_SESSIONS` remains an explicit deployment setting (Compose default:
10). For a higher-capacity production host, validate a synthetic ceiling on
that same host and configure no more than 80% of the last fully stable level;
for a business target of 100, the 125-session gate must pass first.

## Build

### Local (Mac / development):

```bash
docker build -t clawith-aio-sandbox:1.9.3-patched docker/aio-sandbox/
```

### Production (linux/amd64 — required for prod servers):

```bash
docker buildx build --platform linux/amd64 \
  -t <registry>/clawith-aio-sandbox:1.9.3-patched \
  --push \
  docker/aio-sandbox/
```
