from app.services.mcp_client import MCPClient


def test_streamable_http_url_preserves_trailing_slash():
    client = MCPClient("https://example.test/mcp/")

    assert client.server_url == "https://example.test/mcp/"


def test_api_key_extraction_preserves_path_and_remaining_query():
    client = MCPClient("https://example.test/mcp/?apiKey=secret&region=cn")

    assert client.api_key == "secret"
    assert client.server_url == "https://example.test/mcp/?region=cn"


def test_legacy_sse_url_has_exactly_one_separator():
    assert MCPClient("https://example.test/mcp/")._legacy_sse_url() == "https://example.test/mcp/sse"
    assert MCPClient("https://example.test/mcp/sse/")._legacy_sse_url() == "https://example.test/mcp/sse"


def test_empty_transport_error_uses_exception_class_name():
    assert MCPClient._error_message(TimeoutError()) == "TimeoutError"
