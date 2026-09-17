from app.services.llm.client_shared import *  # noqa: F401,F403
from copy import deepcopy

from app.services.llm.responses_stream import stream_response
from app.services.llm.provider_parameters import merge_request_headers


def _is_empty_assistant_message(item: dict[str, Any]) -> bool:
    if item.get("role") != "assistant":
        return False
    content = item.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if not isinstance(content, list):
        return False
    if not content:
        return True
    for part in content:
        if not isinstance(part, dict):
            if part:
                return False
            continue
        part_type = part.get("type")
        if part_type not in {"output_text", "input_text", "text", "refusal"}:
            return False
        value = part.get("refusal") if part_type == "refusal" else part.get("text")
        if str(value or "").strip():
            return False
    return True


class OpenAIResponsesClient(LLMClient):
    """Client for OpenAI Responses API (`/v1/responses`)."""

    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        supports_tool_choice: bool = True,
        provider_managed_timeout: bool = False,
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(
            api_key,
            base_url or self.DEFAULT_BASE_URL,
            model,
            timeout,
            provider_managed_timeout,
            extra_headers,
        )
        self.supports_tool_choice = supports_tool_choice
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=_httpx_timeout(
                    self.timeout,
                    provider_managed_timeout=self.provider_managed_timeout,
                ),
                follow_redirects=True,
                event_hooks=self._request_event_hooks(),
                proxy=None,
            )
        return self._client

    def _get_headers(self) -> dict[str, str]:
        return merge_request_headers({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }, self.extra_headers)

    def _normalize_base_url(self) -> str:
        """Normalize base URL by stripping trailing /responses endpoint."""
        url = self.base_url.rstrip("/")
        if url.endswith("/responses"):
            url = url[: -len("/responses")]
        return url

    def _format_content_for_input(self, content: Any) -> Any:
        """Convert OpenAI chat-style content into Responses API input content."""
        if not isinstance(content, list):
            return content

        formatted: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                formatted.append({"type": "input_text", "text": part.get("text", "")})
            elif ptype == "image_url":
                img = part.get("image_url", {})
                if isinstance(img, dict):
                    formatted.append({"type": "input_image", "image_url": img.get("url", "")})
            elif ptype == "file":
                formatted.append({"type": "input_file", **part["file"]})
            else:
                formatted.append(part)
        return formatted if formatted else content

    def _messages_to_input(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """Convert canonical message format to Responses API input format."""
        input_items: list[dict[str, Any]] = []

        for msg in messages:
            snapshot = msg.responses_snapshot
            if (
                msg.role == "assistant"
                and isinstance(snapshot, dict)
                and snapshot.get("protocol") == "openai_responses"
                and snapshot.get("endpoint") == self._normalize_base_url()
                and snapshot.get("model") == self.model
                and isinstance(snapshot.get("output"), list) and snapshot["output"]
            ):
                native_items = deepcopy(snapshot["output"])
                canonical_calls = {call.get("id"): call for call in msg.tool_calls or []}
                for item in native_items:
                    call = canonical_calls.get(item.get("call_id"))
                    if item.get("type") == "function_call" and call:
                        # The shared loop can repair malformed JSON arguments.
                        # Keep that repair while retaining native ids and phase.
                        args = (call.get("function") or {}).get("arguments")
                        try:
                            json.loads(item.get("arguments", "{}"))
                        except (ValueError, TypeError):
                            if args is None:
                                continue
                            item["arguments"] = (
                                json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else args
                            )
                input_items.extend(native_items)
                continue
            # Handle system messages with dynamic_content
            if msg.role == "system" and msg.content is not None:
                content = msg.content
                if msg.dynamic_content:
                    content = f"{content}\n\n{msg.dynamic_content}"
                input_items.append({
                    "role": msg.role,
                    "content": self._format_content_for_input(content),
                })
            elif msg.role in {"user", "assistant"} and msg.content is not None:
                input_items.append({
                    "role": msg.role,
                    "content": self._format_content_for_input(msg.content),
                })

            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": str(args or "{}"),
                    })

            if msg.role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id or "",
                    "output": self._format_content_for_input(msg.content) if isinstance(msg.content, list) else msg.content or "",
                })

        # Sanitize: ensure every function_call_output has a matching function_call.
        # This prevents "No tool call found for function call output" API errors
        # caused by context window truncation breaking assistant+tool pairs.
        input_items = self._sanitize_input_items(input_items)

        return input_items

    @staticmethod
    def _sanitize_input_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove empty assistant messages and incomplete function-call pairs.

        Some compatible Responses providers reject an assistant ``message``
        whose content is empty. Native response snapshots can contain such an
        item next to a valid function call, so sanitize the final wire input.
        """
        items = [item for item in items if not _is_empty_assistant_message(item)]

        # Collect all call_ids from function_call items
        call_ids_with_fc: set[str] = set()
        for item in items:
            if item.get("type") == "function_call":
                call_id = item.get("call_id", "")
                if call_id:
                    call_ids_with_fc.add(call_id)

        # Collect all call_ids from function_call_output items
        call_ids_with_fco: set[str] = set()
        for item in items:
            if item.get("type") == "function_call_output":
                call_id = item.get("call_id", "")
                if call_id:
                    call_ids_with_fco.add(call_id)

        # Determine which call_ids are orphaned (output without call, or call without output)
        orphaned_fco = call_ids_with_fco - call_ids_with_fc
        orphaned_fc = call_ids_with_fc - call_ids_with_fco

        if not orphaned_fco and not orphaned_fc:
            return items

        if orphaned_fco:
            logger.warning(
                "[OpenAIResponses] Removing %d orphaned function_call_output item(s) "
                "with no matching function_call: %s",
                len(orphaned_fco),
                orphaned_fco,
            )
        if orphaned_fc:
            logger.warning(
                "[OpenAIResponses] Removing %d orphaned function_call item(s) "
                "with no matching function_call_output: %s",
                len(orphaned_fc),
                orphaned_fc,
            )

        # Filter out orphaned items
        return [
            item for item in items
            if not (
                (item.get("type") == "function_call_output" and item.get("call_id", "") in orphaned_fco)
                or (item.get("type") == "function_call" and item.get("call_id", "") in orphaned_fc)
            )
        ]
    def _convert_tools(self, tools: list[dict] | None) -> list[dict] | None:
        """Convert OpenAI tool schema to Responses API function tool schema."""
        if not tools:
            return None

        converted: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            fn = tool.get("function", {})
            converted.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object"}),
                "strict": fn.get("strict", False),
            })
        return converted or None

    def _build_payload(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build request payload."""
        from app.services.llm.reasoning import openai_responses_reasoning_options

        reasoning_effort = kwargs.pop("reasoning_effort", None)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": self._messages_to_input(messages),
            "stream": stream,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if temperature is not None:
            payload["temperature"] = temperature

        if max_tokens:
            payload["max_output_tokens"] = max_tokens

        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
            if self.supports_tool_choice:
                payload["tool_choice"] = "auto"

        payload.update(
            openai_responses_reasoning_options(
                provider="openai",
                model=self.model,
                base_url=self.base_url,
                effort=reasoning_effort,
            )
        )

        payload.update(kwargs)
        return payload

    def _parse_response_data(self, data: dict[str, Any]) -> LLMResponse:
        """Convert Responses API payload into canonical LLMResponse."""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for item in data.get("output", []) or []:
            item_type = item.get("type")
            if item_type == "message":
                for c in item.get("content", []) or []:
                    c_type = c.get("type")
                    if c_type in {"output_text", "text"}:
                        content_parts.append(c.get("text", ""))
                    elif c_type == "refusal":
                        content_parts.append(c.get("refusal", ""))
                    elif c_type == "reasoning":
                        reasoning_parts.append(c.get("summary", "") or c.get("text", ""))
            elif item_type == "function_call":
                args = item.get("arguments", "{}")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                tool_calls.append({
                    "id": item.get("call_id") or item.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": str(args or "{}"),
                    },
                })
            elif item_type == "reasoning":
                reasoning_parts.extend(
                    part.get("text", "") for part in item.get("summary", [])
                    if isinstance(part, dict)
                )

        # Some Responses payloads include a pre-aggregated output_text field.
        # Use it as a fallback when output blocks are empty.
        if not content_parts and data.get("output_text"):
            content_parts.append(str(data.get("output_text", "")))

        usage = data.get("usage")
        finish_reason = "tool_calls" if tool_calls else "stop"

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts) or None,
            finish_reason=finish_reason,
            usage=usage if isinstance(usage, dict) else None,
            model=data.get("model"),
            responses_snapshot={
                "protocol": "openai_responses",
                "endpoint": self._normalize_base_url(),
                "model": self.model,
                "response_id": data.get("id"),
                "output": deepcopy(data.get("output") or []),
            },
        )

    def _extract_api_error(self, data: dict[str, Any]) -> str | None:
        """Extract meaningful error message from Responses API payload."""
        # OpenAI Responses often returns `"error": null` on success,
        # so we must only treat it as error when it's truthy.
        err = data.get("error")
        if err:
            if isinstance(err, dict):
                msg = err.get("message") or str(err)
                err_type = err.get("type")
                err_code = err.get("code")
                extra = []
                if err_type:
                    extra.append(f"type={err_type}")
                if err_code:
                    extra.append(f"code={err_code}")
                suffix = f" ({', '.join(extra)})" if extra else ""
                return f"{msg}{suffix}"
            return str(err)

        status = str(data.get("status") or "").lower()
        if status and status != "completed":
            last_error = data.get("last_error")
            incomplete = data.get("incomplete_details")
            rid = data.get("id")
            details: list[str] = [f"status={status}"]
            if rid:
                details.append(f"id={rid}")
            if last_error:
                details.append(f"last_error={last_error}")
            if incomplete:
                details.append(f"incomplete_details={incomplete}")
            return "Responses API returned non-success status: " + "; ".join(details)

        return None

    def _build_error_log_context(self, data: dict[str, Any]) -> dict[str, Any]:
        """Build compact context for error logs."""
        return {
            "provider": "openai-response",
            "model": self.model,
            "response_id": data.get("id"),
            "status": data.get("status"),
            "incomplete_details": data.get("incomplete_details"),
            "last_error": data.get("last_error"),
            "has_output": bool(data.get("output")),
        }

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Non-streaming completion."""
        url = f"{self._normalize_base_url()}/responses"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=False, **kwargs)

        client = await self._get_client()
        response = await client.post(url, json=payload, headers=self._get_headers())

        if response.status_code >= 400:
            error_text = response.text
            raise LLMError.from_http(response.status_code, error_text, response.headers)

        data = response.json()
        api_error = self._extract_api_error(data)
        if api_error:
            ctx = self._build_error_log_context(data)
            logger.error(
                "OpenAIResponses API error: %s | context=%s",
                api_error,
                ctx,
            )
            raise LLMError.from_payload(data)

        return self._parse_response_data(data)

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_chunk: ChunkCallback | None = None,
        on_tool_delta: ToolCallback | None = None,
        on_thinking: ThinkingCallback | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Deliver SSE deltas immediately and return only a completed response."""
        payload = self._build_payload(
            messages, tools, temperature, max_tokens, stream=True, **kwargs,
        )
        return await stream_response(
            self, payload, on_chunk=on_chunk,
            on_tool_delta=on_tool_delta, on_thinking=on_thinking,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

__all__ = (
    'OpenAIResponsesClient',
)
