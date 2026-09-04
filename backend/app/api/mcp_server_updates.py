"""Small normalization helpers for MCP server and override writes."""

from fastapi import HTTPException

from app.services.mcp_secret_fields import preserve_masked_env, preserve_masked_headers


def normalize_masked_secret_update(
    update_data: dict,
    current_headers: dict | None,
    current_env: dict | None,
) -> dict:
    if update_data.get("headers_template") is not None:
        try:
            update_data["headers_template"] = preserve_masked_headers(
                update_data["headers_template"],
                current_headers,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if update_data.get("env_template") is None:
        return update_data
    try:
        update_data["env_template"] = preserve_masked_env(
            update_data["env_template"],
            current_env,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return update_data
