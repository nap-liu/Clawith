"""Responses SSE transport; only a terminal response authorizes tool execution."""

import json

from app.services.llm.client_shared import LLMError


async def stream_response(client, payload, *, on_chunk, on_tool_delta, on_thinking):
    http = await client._get_client()
    terminal = None
    calls = {}
    data_lines = []

    async def consume(raw):
        nonlocal terminal
        if raw == "[DONE]":
            return
        try:
            event = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise LLMError("Invalid Responses stream event") from exc
        kind = event.get("type", "")
        if kind == "error":
            raise LLMError(client._extract_api_error({"error": event}) or "Responses stream error")
        if kind in {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}:
            terminal = event.get("response") or {}
            error = client._extract_api_error(terminal)
            if error:
                raise LLMError(error)
            if kind != "response.completed" or terminal.get("status") != "completed":
                raise LLMError("Responses stream did not complete successfully")
        elif kind in {"response.output_text.delta", "response.refusal.delta"} and on_chunk:
            await on_chunk(event.get("delta") or "")
        elif kind == "response.reasoning_summary_text.delta" and on_thinking:
            await on_thinking(event.get("delta") or "")
        elif kind == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                index = event.get("output_index", 0)
                calls[index] = {
                    "id": item.get("call_id") or item.get("id"),
                    "index": index,
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                }
                if on_tool_delta:
                    await on_tool_delta(dict(calls[index]))
        elif kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            index = event.get("output_index", 0)
            call = calls.setdefault(index, {
                "id": event.get("item_id"), "index": index, "name": "", "arguments": "",
            })
            if kind.endswith(".delta"):
                call["arguments"] += event.get("delta") or ""
            else:
                call["arguments"] = event.get("arguments", call["arguments"])
            if on_tool_delta:
                await on_tool_delta(dict(call))

    async with http.stream(
        "POST", f"{client._normalize_base_url()}/responses",
        json=payload, headers=client._get_headers(),
    ) as response:
        if response.status_code >= 400:
            body = await response.aread()
            raise LLMError.from_http(response.status_code, body.decode(errors="replace")[:500])
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            elif not line and data_lines:
                await consume("\n".join(data_lines))
                data_lines.clear()
                if terminal is not None:
                    break
        if data_lines and terminal is None:
            await consume("\n".join(data_lines))
    if terminal is None:
        raise LLMError("Responses stream ended before response.completed")
    return client._parse_response_data(terminal)
