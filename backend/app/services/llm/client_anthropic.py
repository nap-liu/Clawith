from app.services.llm.client_shared import *  # noqa: F401,F403

class AnthropicClient(LLMClient):
    """Client for Anthropic's native Messages API.

    Supports Claude 3.x and Claude 3.7+ with extended thinking.
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        provider_managed_timeout: bool = False,
    ):
        super().__init__(
            api_key,
            base_url or self.DEFAULT_BASE_URL,
            model,
            timeout,
            provider_managed_timeout,
        )
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
                proxy=None,
            )
        return self._client

    def _get_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
            "anthropic-beta": "prompt-caching-2024-07-31",
        }

    def _normalize_base_url(self) -> str:
        """Normalize base URL by stripping trailing API paths."""
        url = self.base_url.rstrip("/")
        if url.endswith("/v1/messages"):
            url = url[: -len("/v1/messages")]
        elif url.endswith("/v1/chat/completions"):
            url = url[: -len("/v1/chat/completions")]
        elif url.endswith("/v1"):
            url = url[: -len("/v1")]
        return url

    def _build_payload(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build Anthropic request payload."""
        system_blocks = []
        anthropic_messages = []

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_blocks.append({
                        "type": "text",
                        "text": msg.content,
                        "cache_control": {"type": "ephemeral"}
                    })
                # NOTE: no longer expected to be populated after context-v2 P1; kept for backward compat
                if msg.dynamic_content:
                    system_blocks.append({
                        "type": "text",
                        "text": f"\n{msg.dynamic_content}"
                    })
            else:
                formatted = msg.to_anthropic_format()
                if formatted:
                    anthropic_messages.append(formatted)

        # Prompt-cache breakpoint on messages:
        # The last message is the current turn (contains volatile <context> + user input),
        # so caching it would write a cache entry that never hits. Instead, place the
        # cache_control on the tail of the stable prefix — that is, messages[-2] — which
        # covers everything the next turn will share (history + static system + tools).
        # On the first turn (len < 2) there is no stable prefix to cache yet, so skip.
        if len(anthropic_messages) >= 2:
            prefix_msg = anthropic_messages[-2]
            prefix_content = prefix_msg.get("content")
            if isinstance(prefix_content, list) and prefix_content:
                # For tool_result blocks, cache_control goes on the tool_result block itself
                # (at the top level), NOT nested inside its own .content list.
                prefix_content[-1]["cache_control"] = {"type": "ephemeral"}
            elif isinstance(prefix_content, str):
                prefix_msg["content"] = [
                    {
                        "type": "text",
                        "text": prefix_content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or 4096,
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        if system_blocks:
            payload["system"] = system_blocks

        # Handle Extended Thinking
        thinking = kwargs.pop("thinking", None)
        if thinking:
            payload["thinking"] = thinking
            # For thinking models, temperature must be 1.0 or omitted in some cases
            # But usually it's best to let user specify or default to 1.0 if not set
            if "temperature" not in kwargs:
                payload["temperature"] = 1.0

        if tools:
            anthropic_tools = []
            for tool in tools:
                if tool.get("type") == "function":
                    func = tool["function"]
                    anthropic_tools.append({
                        "name": func["name"],
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {"type": "object"}),
                    })
            if anthropic_tools:
                anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = anthropic_tools

        payload.update(kwargs)
        return payload

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Non-streaming completion."""
        url = f"{self._normalize_base_url()}/v1/messages"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=False, **kwargs)

        client = await self._get_client()
        response = await client.post(url, json=payload, headers=self._get_headers())

        if response.status_code >= 400:
            error_text = response.text[:500]
            raise LLMError.from_http(response.status_code, error_text)

        data = response.json()
        if data.get("type") == "error":
            raise LLMError(f"API error: {data.get('error', {})}")

        full_content = ""
        full_reasoning = ""
        full_signature = None
        tool_calls = []

        for block in data.get("content", []):
            if block.get("type") == "text":
                full_content += block.get("text", "")
            elif block.get("type") == "thinking":
                full_reasoning += block.get("thinking", "")
                full_signature = block.get("signature")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)
                    }
                })

        usage = None
        if "usage" in data:
            usage = {
                "input_tokens": data["usage"].get("input_tokens", 0),
                "output_tokens": data["usage"].get("output_tokens", 0),
                "cache_creation_input_tokens": data["usage"].get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": data["usage"].get("cache_read_input_tokens", 0),
            }

        return LLMResponse(
            content=full_content,
            tool_calls=tool_calls,
            reasoning_content=full_reasoning or None,
            reasoning_signature=full_signature,
            finish_reason=data.get("stop_reason"),
            usage=usage,
            model=data.get("model"),
        )

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
        """Streaming completion."""
        url = f"{self._normalize_base_url()}/v1/messages"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=True, **kwargs)

        full_content = ""
        full_reasoning = ""
        full_signature = None
        tool_calls_data: list[dict] = []
        tool_call_index_map: dict[int, int] = {}
        last_finish_reason: str | None = None
        final_usage = None
        final_model = self.model

        client = await self._get_client()

        try:
            async with client.stream("POST", url, json=payload, headers=self._get_headers()) as resp:
                if resp.status_code >= 400:
                    error_body = ""
                    async for chunk in resp.aiter_bytes():
                        error_body += chunk.decode(errors="replace")
                    raise LLMError.from_http(resp.status_code, error_body[:500])

                current_event = None

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue

                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip()
                        continue

                    if not line.startswith("data:"):
                        continue

                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # Handle events
                    if current_event == "message_start":
                        msg = data.get("message", {})
                        if msg.get("model"):
                            final_model = msg["model"]
                        if msg.get("usage"):
                            final_usage = msg["usage"]

                    elif current_event == "content_block_start":
                        block = data.get("content_block", {})
                        idx = data.get("index", 0)
                        if block.get("type") == "tool_use":
                            tool_call_index_map[idx] = len(tool_calls_data)
                            tool_calls_data.append({
                                "id": block.get("id"),
                                "type": "function",
                                "function": {"name": block.get("name"), "arguments": ""}
                            })
                            if on_tool_delta:
                                await on_tool_delta(
                                    {
                                        "id": block.get("id") or f"draft-{idx}",
                                        "index": idx,
                                        "name": block.get("name", ""),
                                        "arguments": "",
                                    }
                                )

                    elif current_event == "content_block_delta":
                        idx = data.get("index", 0)
                        delta = data.get("delta", {})
                        delta_type = delta.get("type")

                        if delta_type == "text_delta":
                            text = delta.get("text", "")
                            full_content += text
                            if on_chunk:
                                await on_chunk(text)

                        elif delta_type == "thinking_delta":
                            thought = delta.get("thinking", "")
                            full_reasoning += thought
                            if on_thinking:
                                await on_thinking(thought)

                        elif delta_type == "signature_delta":
                            full_signature = delta.get("signature")

                        elif delta_type == "input_json_delta":
                            if idx in tool_call_index_map:
                                tc_idx = tool_call_index_map[idx]
                                tool_calls_data[tc_idx]["function"]["arguments"] += delta.get("partial_json", "")
                                if on_tool_delta:
                                    await on_tool_delta(
                                        {
                                            "id": tool_calls_data[tc_idx].get("id") or f"draft-{idx}",
                                            "index": idx,
                                            "name": tool_calls_data[tc_idx]["function"].get("name", ""),
                                            "arguments": tool_calls_data[tc_idx]["function"].get("arguments", ""),
                                        }
                                    )

                    elif current_event == "message_delta":
                        delta = data.get("delta", {})
                        if delta.get("stop_reason"):
                            last_finish_reason = delta["stop_reason"]
                        if data.get("usage"):
                            # Anthropic's message_delta commonly contains only
                            # cumulative output_tokens. Merge it with the
                            # message_start input/cache counters instead of
                            # erasing the authoritative context size.
                            final_usage = {
                                **(final_usage or {}),
                                **data["usage"],
                            }

                    elif current_event == "error":
                        error_info = data.get("error", {})
                        raise LLMError(f"Anthropic stream error ({error_info.get('type')}): {error_info.get('message')}")

                    elif current_event == "message_stop":
                        break

        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout) as e:
            raise LLMError(f"Connection failed: {e}")

        # Normalize stop reason to OpenAI style (optional but helpful for consistency)
        if last_finish_reason == "end_turn":
            last_finish_reason = "stop"
        elif last_finish_reason == "tool_use":
            last_finish_reason = "tool_calls"

        return LLMResponse(
            content=full_content,
            tool_calls=tool_calls_data,
            reasoning_content=full_reasoning or None,
            reasoning_signature=full_signature,
            finish_reason=last_finish_reason,
            usage=final_usage,
            model=final_model,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

__all__ = (
    'AnthropicClient',
)
