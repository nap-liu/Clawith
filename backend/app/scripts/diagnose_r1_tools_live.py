"""Read-only inventory input; two raw HTTP probes bypass platform parsing.

Consumes the existing secure production inventory pipe, selects deepseek-r1 in
memory, and emits only sanitized public choice/tool fields. Never prints or
persists credentials, endpoints, reasoning content, or raw response objects.
"""

import asyncio
import argparse
import json
import logging
import re
import sys

from loguru import logger
import httpx

from app.scripts.validate_model_protocols_live import (
    Reporter, TOOL, assistant, budget, classify, stream, tool_diagnostic,
)
from app.services.llm.client import LLMMessage, create_llm_client


def visible(text):
    text = re.sub(r"<(think|analysis|reasoning)>.*?(?:</\1>|$)", "", text or "",
                  flags=re.S | re.I)
    return text[:400]


def tool_fields(call):
    function = call.get("function") or {}
    return {"index": call.get("index"), "id": call.get("id"), "type": call.get("type"),
            "function": {"name": function.get("name"),
                         "arguments": str(function.get("arguments") or "")[:300]}}


def request_params(model, streaming):
    params = {
        "model": model["model"], "messages": [{"role": "user", "content": (
            "Call acceptance_echo exactly once with text '7341'. Do not answer until "
            "you receive its result, then repeat its returned value exactly."
        )}], "tools": [TOOL], "tool_choice": "auto", "parallel_tool_calls": True,
        "max_tokens": budget(model), "stream": streaming,
    }
    if model.get("temperature") is not None:
        params["temperature"] = model["temperature"]
    if streaming:
        params["stream_options"] = {"include_usage": True}
    return params


async def shared_params(model):
    raw = request_params(model, True)
    client = create_llm_client(model["provider"], model["api_key"], model["model"],
                               model["base_url"], api_protocol="openai_compatible")
    try:
        return client._build_payload(
            messages=[LLMMessage(**message) for message in raw["messages"]], tools=raw["tools"],
            temperature=model.get("temperature"), max_tokens=budget(model), stream=True,
            reasoning_effort=None,
        )
    finally:
        await client.close()


async def compare_payload(model, reporter):
    raw = request_params(model, True)
    shared = await shared_params(model)
    different = sorted(key for key in set(raw) | set(shared) if raw.get(key) != shared.get(key))
    reporter.emit({"event": "payload_comparison", "model": model["model"],
                   "equal": not different, "different_keys": different,
                   "differences": {key: {"raw": raw.get(key), "shared": shared.get(key)} for key in different}})


async def probe(model, reporter, streaming, shared_payload=False):
    params = await shared_params(model) if shared_payload else request_params(model, streaming)
    result = {"event": "raw_http", "mode": "stream" if streaming else "nonstream",
              "model": model["model"], "id": model.get("id"), "max_retries": 0,
              "shared_payload": shared_payload}
    try:
        async with asyncio.timeout(180), httpx.AsyncClient(timeout=180) as client:
            endpoint = model["base_url"].rstrip("/") + "/chat/completions"
            headers = {"Authorization": f"Bearer {model['api_key']}"}
            choices = {}
            if streaming:
                async with client.stream("POST", endpoint, headers=headers, json=params) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            result["done_received"] = True
                            break
                        chunk = json.loads(raw)
                        if chunk.get("error"):
                            raise RuntimeError(str(chunk["error"].get("message") or "Provider error event"))
                        for choice in chunk.get("choices", []):
                            index = choice.get("index", 0)
                            out = choices.setdefault(index, {
                                "index": index, "finish_reason": None,
                                "content": "", "tool_deltas": [], "chunks": 0,
                            })
                            out["chunks"] += 1
                            delta = choice.get("delta") or {}
                            out["content"] += delta.get("content") or ""
                            if choice.get("finish_reason"):
                                out["finish_reason"] = choice["finish_reason"]
                            for call in delta.get("tool_calls") or []:
                                out["tool_deltas"].append(tool_fields(call))
            else:
                response = await client.post(endpoint, headers=headers, json=params)
                response.raise_for_status()
                for choice in response.json().get("choices", []):
                    message = choice.get("message") or {}
                    index = choice.get("index", 0)
                    choices[index] = {
                        "index": index, "finish_reason": choice.get("finish_reason"),
                        "content": message.get("content") or "",
                        "tool_calls": [tool_fields(call) for call in message.get("tool_calls") or []],
                    }
            public = []
            for out in choices.values():
                content = out.pop("content")
                out["visible_answer"] = visible(content)
                out["visible_chars"] = len(content)
                out["standard_tool_fields_count"] = len(out.get("tool_deltas", out.get("tool_calls", [])))
                public.append(out)
            result.update(ok=True, choices=public)
    except Exception as exc:
        result.update(ok=False, category=classify(f"{type(exc).__name__}: {exc}"),
                      error=f"{type(exc).__name__}: {exc}")
    reporter.emit(result)


async def shared_loop_once(model, reporter):
    client = create_llm_client(model["provider"], model["api_key"], model["model"],
                               model["base_url"], api_protocol="openai_compatible")
    result = {"event": "shared_loop", "model": model["model"], "rounds": [], "tool_executions": 0}
    messages = [LLMMessage(**message) for message in request_params(model, True)["messages"]]
    try:
        response, _ = await stream(client, messages, model, 180, tools=[TOOL])
        result["rounds"].append(tool_diagnostic(response))
        calls = response.tool_calls or []
        if len(calls) != 1:
            raise RuntimeError("Expected exactly one standard tool call")
        call = calls[0]
        function = call.get("function") or {}
        args = function.get("arguments") or "{}"
        args = json.loads(args) if isinstance(args, str) else args
        if function.get("name") != "acceptance_echo" or args != {"text": "7341"}:
            raise RuntimeError("Unexpected tool name or arguments")
        echo = "ECHO_CONFIRMED_" + args["text"]
        result["tool_executions"] = 1
        messages.extend([assistant(response), LLMMessage(role="tool", tool_call_id=call["id"], content=echo)])
        answer, _ = await stream(client, messages, model, 180, tools=[TOOL])
        result["rounds"].append(tool_diagnostic(answer))
        if echo not in (answer.content or "") or answer.tool_calls:
            raise RuntimeError("Tool result continuation failed")
        messages.extend([assistant(answer), LLMMessage(role="user", content=(
            "Without calling tools, repeat the exact echo value from our previous exchange."
        ))])
        followup, _ = await stream(client, messages, model, 180, tools=[TOOL])
        result["rounds"].append(tool_diagnostic(followup))
        if echo not in (followup.content or "") or followup.tool_calls:
            raise RuntimeError("History followup failed")
        result["ok"] = True
    except Exception as exc:
        result.update(ok=False, category=classify(f"{type(exc).__name__}: {exc}"),
                      error=f"{type(exc).__name__}: {exc}")
    finally:
        await client.close()
    reporter.emit(result)


async def run(inventory, reporter, compare_only=False, shared_payload=False, shared_loop=False):
    models = [model for model in inventory if model.get("model") == "deepseek-r1"
              and model.get("enabled") is not False]
    if len(models) != 1:
        raise ValueError("Expected exactly one enabled deepseek-r1 inventory entry")
    if compare_only:
        await compare_payload(models[0], reporter)
        return
    if shared_payload:
        await probe(models[0], reporter, True, shared_payload=True)
        return
    if shared_loop:
        await shared_loop_once(models[0], reporter)
        return
    await probe(models[0], reporter, True)
    await probe(models[0], reporter, False)


def main():
    logger.remove()
    logging.disable(logging.CRITICAL)
    reporter = Reporter([])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-only", action="store_true")
    parser.add_argument("--shared-payload", action="store_true")
    parser.add_argument("--shared-loop-once", action="store_true")
    args = parser.parse_args()
    try:
        inventory = json.load(sys.stdin)
        reporter = Reporter(inventory)
        asyncio.run(run(inventory, reporter, args.compare_only, args.shared_payload, args.shared_loop_once))
    except Exception as exc:
        reporter.emit({"event": "fatal", "error_type": type(exc).__name__})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
