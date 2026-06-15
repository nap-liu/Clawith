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
