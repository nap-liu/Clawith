from __future__ import annotations

import uuid

from app.services.agent_tools_feishu_auth import (
    _check_feishu_err,
    _get_feishu_bitable_url,
    _get_feishu_credentials,
    _parse_feishu_url,
)
from app.services.agent_tools_feishu_docs import _feishu_wiki_get_node


async def _resolve_bitable_app_token(agent_id: uuid.UUID, parsed_url: dict) -> str | None:
    app_token = parsed_url.get("app_token")
    if app_token:
        return app_token
    wiki_token = parsed_url.get("wiki_token")
    if wiki_token:
        app_id, app_secret = await _get_feishu_credentials(agent_id)
        if app_id and app_secret:
            from app.services.feishu_service import feishu_service

            token = await feishu_service.get_tenant_access_token(app_id, app_secret)
            node_info = await _feishu_wiki_get_node(wiki_token, token)
            if node_info and node_info.get("obj_token"):
                return node_info["obj_token"]
    return None


async def _bitable_list_tables(agent_id: uuid.UUID, arguments: dict) -> str:
    """List all tables in a Feishu Bitable app."""
    url = arguments.get("url", "")
    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    if not app_token:
        return "Failed: Could not extract Bitable app_token from the URL (also could not resolve wiki_token)."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_list_tables(app_id, app_secret, app_token)
        err = _check_feishu_err(resp)
        if err:
            return err

        tables = resp.get("data", {}).get("items", [])
        if not tables:
            return "OK: No tables found in this Bitable."
        lines = [f"- {t.get('name')} (ID: {t.get('table_id')})" for t in tables]
        # Provide a user-accessible link so the user can open the Bitable directly
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await _get_feishu_bitable_url(tenant_token, app_token)
        return "OK: Tables in this Bitable:\n" + "\n".join(lines) + f"\n\n🔗 多维表格链接: {bitable_url}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_create_app(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new Feishu Bitable (多维表格) app.

    Calls the Bitable v1 apps API: POST /open-apis/bitable/v1/apps
    The API response includes a user-accessible URL with the tenant's own domain.
    """
    name = arguments.get("name", "").strip()
    if not name:
        return "Failed: Missing required argument 'name' — please provide a name for the new Bitable."

    folder_token = arguments.get("folder_token", "").strip()

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_create_app(app_id, app_secret, name, folder_token)
        err = _check_feishu_err(resp)
        if err:
            return err

        # API response structure: data.app.{app_token, name, url, default_table_id, folder_token}
        app_info = resp.get("data", {}).get("app", {})
        app_token = app_info.get("app_token", "")
        bitable_url = app_info.get("url", "")
        default_table_id = app_info.get("default_table_id", "")
        if not app_token:
            return f"Failed: Bitable created but could not extract app_token from response: {resp}"

        # Fallback URL resolution if the API didn't return one
        if not bitable_url:
            tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
            bitable_url = await _get_feishu_bitable_url(tenant_token, app_token)

        result = f"OK: Bitable created successfully!\nName: {name}\nApp Token: {app_token}\nURL: {bitable_url}"
        if default_table_id:
            result += f"\nDefault Table ID: {default_table_id}"
        return result
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_list_fields(agent_id: uuid.UUID, arguments: dict) -> str:
    """List all fields (columns) in a specific Bitable table."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token:
        return "Failed: Could not extract Bitable app_token from the URL."
    if not table_id:
        return "Failed: table_id is required. Provide it as a parameter or include it in the URL."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_list_fields(app_id, app_secret, app_token, table_id)
        err = _check_feishu_err(resp)
        if err:
            return err

        fields = resp.get("data", {}).get("items", [])
        if not fields:
            return "OK: No fields found in this table."
        lines = [f"- {f.get('field_name')} (type: {f.get('type')}, ID: {f.get('field_id')})" for f in fields]
        return "OK: Fields in this table:\n" + "\n".join(lines)
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_query_records(agent_id: uuid.UUID, arguments: dict) -> str:
    """Query records (rows) from a Bitable table, with optional FQL filter."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")
    filter_info = arguments.get("filter_info", "")
    max_results = arguments.get("max_results", 100)

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token or not table_id:
        return "Failed: Could not resolve app_token or table_id from the provided parameters/URL."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        import json

        filters_dict = {}
        if isinstance(filter_info, dict):
            filters_dict = filter_info
        elif isinstance(filter_info, str) and filter_info.strip():
            try:
                filters_dict = json.loads(filter_info)
            except:
                pass

        resp = await feishu_service.bitable_query_records(app_id, app_secret, app_token, table_id, filters_dict)
        err = _check_feishu_err(resp)
        if err:
            return err

        records = resp.get("data", {}).get("items", [])
        if not records:
            return "OK: No matching records found."

        lines = []
        for r in records[:max_results]:
            lines.append(f"Record {r.get('record_id')}: {json.dumps(r.get('fields', {}), ensure_ascii=False)}")
        return "OK: Query results:\n" + "\n".join(lines)
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_create_record(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new record (row) in a Bitable table."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")
    fields_str = arguments.get("fields", "{}")

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token or not table_id:
        return "Failed: Could not resolve app_token or table_id from the provided parameters/URL."

    import json

    try:
        fields = json.loads(fields_str)
    except json.JSONDecodeError:
        return "Failed: The 'fields' parameter is not valid JSON."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_create_record(app_id, app_secret, app_token, table_id, fields)
        err = _check_feishu_err(resp)
        if err:
            return err

        record = resp.get("data", {}).get("record", {})
        # Provide a user-accessible link so they can verify the new row in the table
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await _get_feishu_bitable_url(tenant_token, app_token, table_id)
        return (
            f"OK: Record created. Record ID: {record.get('record_id')}\n"
            f"Fields: {json.dumps(record.get('fields', {}), ensure_ascii=False)}\n"
            f"🔗 多维表格链接: {bitable_url}"
        )
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_update_record(agent_id: uuid.UUID, arguments: dict) -> str:
    """Update an existing record in a Bitable table by record_id."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")
    record_id = arguments.get("record_id", "")
    fields_str = arguments.get("fields", "{}")

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token or not table_id or not record_id:
        return "Failed: Missing required parameters. Need app_token (from URL), table_id, and record_id."

    import json

    try:
        fields = json.loads(fields_str)
    except json.JSONDecodeError:
        return "Failed: The 'fields' parameter is not valid JSON."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_update_record(app_id, app_secret, app_token, table_id, record_id, fields)
        err = _check_feishu_err(resp)
        if err:
            return err

        record = resp.get("data", {}).get("record", {})
        # Provide a user-accessible link so they can verify the updated row
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await _get_feishu_bitable_url(tenant_token, app_token, table_id)
        return (
            f"OK: Record updated. Record ID: {record.get('record_id')}\n"
            f"Fields: {json.dumps(record.get('fields', {}), ensure_ascii=False)}\n"
            f"🔗 多维表格链接: {bitable_url}"
        )
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_delete_record(agent_id: uuid.UUID, arguments: dict) -> str:
    """Delete a record from a Bitable table by record_id."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")
    record_id = arguments.get("record_id", "")

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token or not table_id or not record_id:
        return "Failed: Missing required parameters. Need app_token (from URL), table_id, and record_id."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_delete_record(app_id, app_secret, app_token, table_id, record_id)
        err = _check_feishu_err(resp)
        if err:
            return err

        # Provide a user-accessible link so they can verify the deletion
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await _get_feishu_bitable_url(tenant_token, app_token, table_id)
        return f"OK: Record {record_id} deleted successfully.\n🔗 多维表格链接: {bitable_url}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


__all__ = [
    "_resolve_bitable_app_token",
    "_bitable_list_tables",
    "_bitable_create_app",
    "_bitable_list_fields",
    "_bitable_query_records",
    "_bitable_create_record",
    "_bitable_update_record",
    "_bitable_delete_record",
]
