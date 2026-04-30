"""Parse MCP server config from heterogeneous user input.

Accepts:
  • bare URL string (http/https)
  • {"url": "...", "headers": {...}, ...}                    single-server spec
  • {"mcpServers": {"<name>": {"url"|"command", ...}}}       standard MCP config
  • JSON-stringified versions of any of the above

Returns a dict with keys: url, name, headers, api_key, error.
- url      : str | None — the remote endpoint (None for stdio servers)
- name     : str | None — display hint
- headers  : dict | None — extra HTTP headers
- api_key  : str | None — bearer token (auto-extracted from headers when present)
- error    : str | None — set when input is recognized but unsupported (e.g. stdio)
"""

import json
from urllib.parse import urlparse


def _is_url(s: str) -> bool:
    if not isinstance(s, str):
        return False
    try:
        p = urlparse(s.strip())
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _from_server_spec(spec: dict, name_hint: str | None = None) -> dict:
    """Normalize a single MCP server spec dict."""
    if not isinstance(spec, dict):
        return {"url": None, "error": "MCP server spec must be an object"}

    if "command" in spec and "url" not in spec:
        return {
            "url": None,
            "name": name_hint or spec.get("name"),
            "error": "stdio MCP servers (command/args) are not supported in this deployment — pass a remote `url` instead",
        }

    url = spec.get("url") or spec.get("endpoint")
    if not url or not _is_url(url):
        return {"url": None, "error": "MCP server spec missing a valid http/https `url`"}

    headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else None

    # Pull bearer token out of Authorization header so MCPClient can use it
    api_key = None
    if headers:
        auth = headers.get("Authorization") or headers.get("authorization")
        if isinstance(auth, str) and auth.lower().startswith("bearer "):
            api_key = auth.split(None, 1)[1].strip()
            # Leave the header in place — MCPClient will use api_key first;
            # if the server expects a non-bearer header style, it still flows through.

    return {
        "url": url.strip(),
        "name": name_hint or spec.get("name"),
        "headers": headers,
        "api_key": api_key,
        "error": None,
    }


def parse_mcp_input(value) -> dict | None:
    """Best-effort parse of a single MCP server config from user input.

    Returns None when input is empty / unrecognizable. Returns a dict with
    `error` set when the shape is recognizable but unsupported.
    """
    if value is None:
        return None

    # 1) Strings: bare URL or JSON
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if _is_url(s):
            return {"url": s, "name": None, "headers": None, "api_key": None, "error": None}
        if s[:1] in ("{", "["):
            try:
                value = json.loads(s)
            except json.JSONDecodeError:
                return None
        else:
            return None

    # 2) Lists: not currently supported (multi-server)
    if isinstance(value, list):
        return {"url": None, "error": "Multiple MCP servers in one import are not supported — pass a single server config"}

    # 3) Dicts
    if isinstance(value, dict):
        # Standard MCP config: {"mcpServers": {"<name>": {...}}}
        servers = value.get("mcpServers")
        if isinstance(servers, dict) and servers:
            if len(servers) > 1:
                # Pick the first deterministically but flag a hint via name
                name = next(iter(servers))
                return _from_server_spec(servers[name], name_hint=name) | {
                    "_warning": f"Multiple servers in mcpServers; only `{name}` was imported",
                }
            name, spec = next(iter(servers.items()))
            return _from_server_spec(spec, name_hint=name)

        # Single-server spec passed directly
        if "url" in value or "command" in value or "endpoint" in value:
            return _from_server_spec(value)

    return None
