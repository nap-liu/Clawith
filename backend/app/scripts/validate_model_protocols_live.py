"""Probe an in-memory model inventory through the local API and shared clients.

Run only in the isolated acceptance Docker environment. stdin is a JSON list;
credentials are never persisted. stdout contains one sanitized JSON event per
stage and a final summary. This is a client/API probe, not a durable turn test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import asdict
from datetime import timedelta

import httpx
from loguru import logger
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session
from app.models.user import User
from app.services.llm.client import LLMMessage, create_llm_client
from app.services.llm.reasoning import resolve_reasoning_capability

TOOL = {"type": "function", "function": {
    "name": "acceptance_echo", "description": "Return a deterministic local echo result.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"], "additionalProperties": False},
}}


def classify(message: str, status: int | None = None) -> str:
    text = message.lower()
    embedded = re.search(r"(?:http\s+|error code:\s*)(\d{3})\b", text)
    if embedded:
        status = int(embedded[1])
    if status in {401, 403} or any(word in text for word in (
        "unauthorized", "invalid api key", "authentication", "permission denied", "access denied",
    )):
        return "authentication_or_permission"
    if "incomplete" in text or "max_output_tokens" in text or "token limit" in text:
        return "output_budget_or_incomplete"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if status == 429 or "rate limit" in text or "quota" in text:
        return "rate_limit_or_quota"
    if (re.search(r"\bunsupported\s+model\s*:", text)
            or re.search(r"responses?.{0,90}(not supported|unsupported|not available|not implemented)", text)
            or re.search(r"(not supported|unsupported).{0,90}responses?", text)
            or (status in {404, 405, 501} and any(word in text for word in (
                "endpoint", "route", "method", "not found", "not implemented",
            )) and not any(word in text for word in ("model", "deployment")))):
        return "protocol_unsupported"
    return "provider_or_request_error"


class Reporter:
    def __init__(self, inventory):
        self.secrets = sorted({str(item[key]) for item in inventory
                               for key in ("api_key", "base_url") if item.get(key)},
                              key=len, reverse=True)

    def clean(self, value):
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if not isinstance(value, str):
            return value
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
            if len(secret) >= 8 and "://" not in secret:
                # Provider error text may already have truncated a credential.
                value = re.sub(re.escape(secret[:8]) + r"[A-Za-z0-9_.\-]*", "[redacted]", value)
        value = re.sub(r"https?://[^\s\"'<>]+", "[endpoint]", value)
        value = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", value, flags=re.I)
        return value

    def emit(self, value):
        print(json.dumps(self.clean(value), ensure_ascii=False), flush=True)


async def local_token() -> str:
    async with async_session() as db:
        user = await db.scalar(select(User).where(
            User.is_active.is_(True), User.role == "platform_admin",
        ).order_by(User.id).limit(1))
        if user is None:
            raise RuntimeError("No active local platform administrator")
        return create_access_token(str(user.id), user.role, timedelta(hours=2))


def budget(model) -> int:
    configured = model.get("max_output_tokens")
    return min(8192, int(configured)) if configured is not None else 8192


async def api_probe(http, model, protocol):
    started = time.monotonic()
    try:
        response = await http.post("/api/enterprise/llm-test", json={
            "provider": model["provider"], "model": model["model"],
            "api_key": model["api_key"], "base_url": model.get("base_url"),
            "api_protocol": protocol, "max_output_tokens": budget(model),
            "reasoning_effort": model.get("reasoning_effort"),
        })
        data = response.json()
        ok = response.is_success and data.get("success") is True
        # Do not print reply/provider bodies. Only the API's dedicated error is used.
        error = str(data.get("error") or data.get("detail") or "") if not ok else ""
        embedded_status = re.search(r"(?:HTTP\s+|Error code:\s*)(\d{3})\b", error, re.I)
        status = int(embedded_status[1]) if embedded_status else response.status_code
        result = {"stage": "enterprise_api", "protocol": protocol, "ok": ok,
                  "http_status": response.status_code, "api_output_cap": min(1024, budget(model))}
        if not ok:
            result.update(category=classify(error, status), error=error)
    except Exception as exc:
        result = {"stage": "enterprise_api", "protocol": protocol, "ok": False,
                  "category": classify(f"{type(exc).__name__}: {exc}"), "error": f"{type(exc).__name__}: {exc}"}
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def assistant(response) -> LLMMessage:
    # In-memory serialization exercises the public message snapshot contract;
    # it does not claim to validate the database persistence/turn loop.
    return LLMMessage(**json.loads(json.dumps(asdict(LLMMessage(
        role="assistant", content=response.content, tool_calls=response.tool_calls or None,
        reasoning_content=response.reasoning_content,
        reasoning_signature=response.reasoning_signature,
        responses_snapshot=response.responses_snapshot,
    )))))


def tool_diagnostic(response) -> dict:
    """Only public echo-test output; never reasoning fields or native items."""
    visible = response.content or ""
    visible = re.sub(r"<(think|analysis|reasoning)>.*?(?:</\1>|$)", "", visible,
                     flags=re.S | re.I)
    calls = response.tool_calls or []
    return {"tool_count": len(calls),
            "tool_names": [call.get("function", {}).get("name") for call in calls],
            "tool_types": [call.get("type") for call in calls],
            "finish_reason": response.finish_reason, "visible_answer": visible[:400]}


async def stream(client, messages, model, timeout, *, effort=None, tools=None):
    chunks = 0
    first_ms = None
    started = time.monotonic()

    async def on_chunk(text):
        nonlocal chunks, first_ms
        if text:
            chunks += 1
            if first_ms is None:
                first_ms = int((time.monotonic() - started) * 1000)

    async with asyncio.timeout(timeout):
        response = await client.stream(
            messages=messages, tools=tools, max_tokens=budget(model),
            temperature=model.get("temperature"), reasoning_effort=effort, on_chunk=on_chunk,
        )
    if response.finish_reason in {"length", "incomplete", "error", "failed"}:
        raise RuntimeError(f"Provider terminal: {response.finish_reason}")
    return response, {"latency_ms": int((time.monotonic() - started) * 1000),
                      "text_chunks": chunks, "first_text_ms": first_ms,
                      "output_chars": len(response.content or ""),
                      "snapshot_present": bool(response.responses_snapshot)}


async def shared_checks(model, protocol, timeout, emit, *, temperature_probe=None):
    client = create_llm_client(
        model["provider"], model["api_key"], model["model"], model.get("base_url"),
        timeout=timeout, api_protocol=protocol,
    )
    results = []
    tool_observations = {}

    async def stage(name, operation, *, capability_only=False):
        try:
            detail = await operation()
            result = {"stage": name, "ok": True, **detail}
        except Exception as exc:
            result = {"stage": name, "ok": False, "category": classify(f"{type(exc).__name__}: {exc}"),
                      "error": f"{type(exc).__name__}: {exc}"}
        if name == "shared_tool_and_history":
            result["observed_rounds"] = dict(tool_observations)
        if capability_only:
            result["capability_only"] = True
        results.append(result)
        emit(result)

    async def plain(effort=None, *, probe=False):
        target = {**model, "temperature": temperature_probe} if probe else model
        response, detail = await stream(client, [LLMMessage(
            role="user", content="Reply with exactly PROBE_OK.",
        )], target, timeout, effort=effort)
        if "PROBE_OK" not in (response.content or "") or detail["text_chunks"] == 0:
            raise RuntimeError("Expected visible streaming answer missing")
        return {**detail, "reasoning_effort": effort, "temperature": target.get("temperature")}

    async def tool_conversation():
        messages = [LLMMessage(role="user", content=(
            "Call acceptance_echo exactly once with text '7341'. Do not answer until "
            "you receive its result, then repeat its returned value exactly."
        ))]
        response, first = await stream(client, messages, model, timeout, tools=[TOOL])
        tool_observations["tool_request"] = tool_diagnostic(response)
        calls = response.tool_calls or []
        if len(calls) != 1:
            raise RuntimeError("Expected exactly one tool call")
        call = calls[0]
        function = call.get("function", {})
        arguments = function.get("arguments", "{}")
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        if function.get("name") != "acceptance_echo" or arguments != {"text": "7341"} or not call.get("id"):
            raise RuntimeError("Unexpected tool name, arguments or call id")
        if protocol == "openai_responses" and not response.responses_snapshot:
            raise RuntimeError("Responses tool round did not return a native snapshot")
        # The only executable operation is this deterministic local function.
        echo = "ECHO_CONFIRMED_" + arguments["text"]
        messages += [assistant(response), LLMMessage(role="tool", tool_call_id=call["id"], content=echo)]
        # The normal platform loop sends the same available schemas every round.
        # Keep tool choice automatic; never force a tool to conceal a missed call.
        answer, second = await stream(client, messages, model, timeout, tools=[TOOL])
        tool_observations["tool_result_continuation"] = tool_diagnostic(answer)
        if echo not in (answer.content or "") or answer.tool_calls:
            raise RuntimeError("Tool-result continuation did not return the echo result")
        messages += [assistant(answer), LLMMessage(
            role="user", content="Without calling tools, repeat the exact echo value from our previous exchange.",
        )]
        followup, third = await stream(client, messages, model, timeout, tools=[TOOL])
        tool_observations["history_followup"] = tool_diagnostic(followup)
        if echo not in (followup.content or "") or followup.tool_calls:
            raise RuntimeError("Serialized history followup lost the tool result")
        return {"tool_executions": 1, "tool_round": first, "continuation": second,
                "history_followup": third, "persistence": "in_memory_serialized"}

    try:
        await stage("shared_stream_auto", plain)
        await stage("shared_tool_and_history", tool_conversation)
        capability = resolve_reasoning_capability(
            provider=model["provider"], model=model["model"], base_url=model.get("base_url"),
        )
        efforts = [item for item in ("minimal", "none") if item in capability.supported_efforts]
        if efforts:
            for effort in efforts:
                await stage(f"shared_reasoning_{effort}", lambda: plain(effort))
        else:
            emit({"stage": "shared_explicit_reasoning", "skipped": True,
                  "reason": "No supported minimal/none setting"})
        if temperature_probe is not None:
            await stage("temperature_capability", lambda: plain(probe=True), capability_only=True)
    finally:
        await client.close()
    return results


async def run_model(model, http, timeout, reporter, *, protocol_mode="auto", temperature_probe=None):
    identity = {key: model.get(key) for key in ("id", "provider", "model", "label")}

    def emit(result):
        reporter.emit({"event": "stage", **identity, **result})

    if model.get("enabled") is False:
        result = {**identity, "skipped": True, "reason": "disabled"}
        reporter.emit({"event": "model", **result})
        return result
    initial_protocol = "openai_compatible" if protocol_mode == "chat" else "openai_responses"
    responses = await api_probe(http, model, initial_protocol)
    emit(responses)
    probes = [responses]
    protocol = initial_protocol
    if not responses["ok"] and protocol_mode == "auto":
        chat = await api_probe(http, model, "openai_compatible")
        emit(chat)
        probes.append(chat)
        if responses.get("category") == "protocol_unsupported" and chat["ok"]:
            protocol = "openai_compatible"
    # A diagnostic Chat success never reclassifies auth/timeout/incomplete as
    # lack of Responses support. Probe that protocol with the larger shared budget.
    try:
        checks = await shared_checks(model, protocol, timeout, emit, temperature_probe=temperature_probe)
    except Exception as exc:
        checks = [{"stage": "shared_client", "ok": False,
                   "category": classify(f"{type(exc).__name__}: {exc}"), "error": f"{type(exc).__name__}: {exc}"}]
        emit(checks[0])
    selected_probe = next(item for item in probes if item["protocol"] == protocol)
    required_checks = [item for item in checks if not item.get("capability_only")]
    result = {**identity, "selected_protocol": protocol, "protocol_mode": protocol_mode,
              "configured_temperature": model.get("temperature"), "api_ok": selected_probe["ok"],
              "shared_ok": bool(required_checks) and all(item["ok"] for item in required_checks),
              "capability_failures": sum(bool(item.get("capability_only")) and not item["ok"] for item in checks),
              "api_probes": probes, "checks": checks, "max_output_tokens": budget(model),
              "configured_reasoning_effort": model.get("reasoning_effort")}
    result["ok"] = result["api_ok"] and result["shared_ok"]
    reporter.emit({"event": "model", **result})
    return result


async def run(inventory, args, reporter):
    token = await local_token()
    reporter.secrets.append(token)
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(base_url=args.api_url.rstrip("/"), timeout=args.timeout + 10,
                                 headers={"Authorization": f"Bearer {token}"}) as http:
        async def bounded(model):
            async with semaphore:
                return await run_model(model, http, args.timeout, reporter,
                                       protocol_mode=args.protocol, temperature_probe=args.temperature_probe)
        results = await asyncio.gather(*(bounded(model) for model in inventory))
    summary = {"models": len(results), "skipped": sum(bool(row.get("skipped")) for row in results),
               "passed": sum(row.get("ok") is True for row in results),
               "failed": sum(row.get("ok") is False for row in results),
               "capability_failures": sum(row.get("capability_failures", 0) for row in results)}
    reporter.emit({"event": "summary", **summary})
    return 1 if summary["failed"] else 0


def main():
    logger.remove()
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Local acceptance origin, without /api")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--protocol", choices=("auto", "chat", "responses"), default="auto",
                        help="Explicit compatibility retest; does not change model configuration")
    parser.add_argument("--model", action="append", default=[],
                        help="Exact model name or ID to include; repeat to select multiple models")
    parser.add_argument("--temperature", default="auto",
                        help="auto: use saved temperature; a number adds one separate capability probe")
    args = parser.parse_args()
    if args.concurrency < 1 or args.timeout <= 0:
        parser.error("concurrency and timeout must be positive")
    try:
        args.temperature_probe = None if args.temperature == "auto" else float(args.temperature)
        if args.temperature_probe is not None and not 0 <= args.temperature_probe <= 2:
            raise ValueError
    except ValueError:
        parser.error("temperature must be auto or a number between 0 and 2")
    reporter = Reporter([])
    try:
        inventory = json.load(sys.stdin)
        if not isinstance(inventory, list) or not inventory:
            raise ValueError("Expected a non-empty JSON model list")
        reporter = Reporter(inventory)
        if args.model:
            if any(not any(name in {str(row.get("id")), row.get("model")} for row in inventory)
                   for name in args.model):
                raise ValueError("Requested model filter has no exact inventory match")
            inventory = [row for row in inventory if any(
                name in {str(row.get("id")), row.get("model")} for name in args.model)]
        for model in inventory:
            if not all(model.get(key) for key in ("provider", "model", "api_key")) and model.get("enabled") is not False:
                raise ValueError("Enabled model missing required connection fields")
            if model.get("enabled") is not False and budget(model) < 1:
                raise ValueError("Output budget must be positive")
        return asyncio.run(run(inventory, args, reporter))
    except Exception as exc:
        # Startup failures may include raw input in library exceptions. Do not echo them.
        reporter.emit({"event": "fatal", "error_type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
