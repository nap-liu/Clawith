"""Tests for app.services.mcp_config_parser.parse_mcp_input."""

import pytest

from app.services.mcp_config_parser import parse_mcp_input


# ── Form A · bare URL string ──────────────────────────────────────

def test_bare_https_url():
    out = parse_mcp_input("https://mcp-gw.dingtalk.com/server/abc?key=xyz")
    assert out["url"] == "https://mcp-gw.dingtalk.com/server/abc?key=xyz"
    assert out["error"] is None
    assert out["headers"] is None


def test_bare_http_url_with_whitespace():
    out = parse_mcp_input("  http://localhost:8080/mcp  ")
    assert out["url"] == "http://localhost:8080/mcp"


def test_non_url_string_returns_none():
    assert parse_mcp_input("dingtalk-mcp") is None
    assert parse_mcp_input("") is None
    assert parse_mcp_input("   ") is None


# ── Form B · single-server spec object ────────────────────────────

def test_single_server_spec_with_headers():
    out = parse_mcp_input({
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer xyz"},
    })
    assert out["url"] == "https://example.com/mcp"
    assert out["headers"] == {"Authorization": "Bearer xyz"}
    assert out["api_key"] == "xyz"  # extracted from Authorization
    assert out["error"] is None


def test_single_server_spec_endpoint_alias():
    out = parse_mcp_input({"endpoint": "https://example.com/mcp"})
    assert out["url"] == "https://example.com/mcp"


def test_single_server_spec_invalid_url():
    out = parse_mcp_input({"url": "not-a-url"})
    assert out["url"] is None
    assert out["error"] is not None


# ── Form C · standard mcpServers config ───────────────────────────

def test_mcpServers_single_entry():
    out = parse_mcp_input({
        "mcpServers": {
            "dingtalk": {
                "url": "https://mcp-gw.dingtalk.com/server/abc",
                "headers": {"X-Tenant": "yyc"},
            }
        }
    })
    assert out["url"] == "https://mcp-gw.dingtalk.com/server/abc"
    assert out["name"] == "dingtalk"
    assert out["headers"] == {"X-Tenant": "yyc"}
    assert out["error"] is None


def test_mcpServers_multiple_entries_picks_first_with_warning():
    out = parse_mcp_input({
        "mcpServers": {
            "first":  {"url": "https://a.example/mcp"},
            "second": {"url": "https://b.example/mcp"},
        }
    })
    # Order-preserving: first entry wins
    assert out["url"] == "https://a.example/mcp"
    assert out["name"] == "first"
    assert "_warning" in out


def test_mcpServers_stdio_command_parsed():
    # stdio servers (command/args) are now supported — parser returns transport=stdio
    out = parse_mcp_input({
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}
        }
    })
    assert out["url"] is None
    assert out.get("error") is None
    assert out.get("transport") == "stdio"
    assert out.get("command") == "npx"


# ── JSON-stringified inputs (LLM tool-calling habit) ──────────────

def test_json_stringified_url_object():
    out = parse_mcp_input('{"url": "https://example.com/mcp"}')
    assert out["url"] == "https://example.com/mcp"


def test_json_stringified_mcpServers():
    out = parse_mcp_input(
        '{"mcpServers": {"x": {"url": "https://example.com/mcp"}}}'
    )
    assert out["url"] == "https://example.com/mcp"
    assert out["name"] == "x"


def test_malformed_json_string_returns_none():
    assert parse_mcp_input('{not json') is None


# ── Edge cases ────────────────────────────────────────────────────

def test_none_input():
    assert parse_mcp_input(None) is None


def test_unsupported_list_input():
    out = parse_mcp_input([{"url": "https://example.com/mcp"}])
    assert out["url"] is None
    assert "Multiple" in (out.get("error") or "")
