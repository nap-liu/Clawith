"""Small normalization helpers for MCP server and override writes."""

from fastapi import HTTPException

from app.services.mcp_secret_fields import preserve_masked_headers


def normalize_masked_header_update(update_data: dict, current_headers: dict | None) -> dict:
    if update_data.get("headers_template") is None:
        return update_data
    try:
        update_data["headers_template"] = preserve_masked_headers(
            update_data["headers_template"],
            current_headers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return update_data
