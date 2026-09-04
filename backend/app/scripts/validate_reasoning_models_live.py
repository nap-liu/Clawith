"""Run live reasoning regression for an exact exported model inventory.

The JSON inventory is read from stdin so credentials never need to be written
to disk. Each item must contain provider, model, base_url, and api_key fields.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

from app.services.llm.client import LLMMessage, create_llm_client
from app.services.llm.reasoning import resolve_reasoning_capability


@dataclass(frozen=True, slots=True)
class LiveModel:
    provider: str
    model: str
    base_url: str | None
    api_key: str
    label: str | None = None
    max_output_tokens: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LiveModel:
        return cls(
            provider=str(raw.get("provider") or "custom"),
            model=str(raw["model"]),
            base_url=str(raw["base_url"]) if raw.get("base_url") else None,
            api_key=str(raw["api_key"]),
            label=str(raw["label"]) if raw.get("label") else None,
            max_output_tokens=(
                int(raw["max_output_tokens"])
                if raw.get("max_output_tokens") is not None
                else None
            ),
        )


def _matrix_token_limit(model: LiveModel, requested: int) -> int:
    if model.max_output_tokens is not None:
        return min(requested, model.max_output_tokens)
    name = model.model.lower()
    if "qwen-max" in name:
        return min(requested, 8_192)
    if "qwen-turbo" in name:
        return min(requested, 16_384)
    if "qwen-plus" in name or "minimax" in name:
        return min(requested, 32_768)
    return requested


def _attempts(
    model: LiveModel,
    *,
    all_efforts: bool,
    matrix_max_tokens: int,
) -> list[tuple[str, str | None, int]]:
    capability = resolve_reasoning_capability(
        provider=model.provider,
        model=model.model,
        base_url=model.base_url,
    )
    if all_efforts:
        max_tokens = _matrix_token_limit(model, matrix_max_tokens)
        return [
            ("default", None, max_tokens),
            *((effort, effort, max_tokens) for effort in capability.supported_efforts),
        ]
    attempts: list[tuple[str, str | None, int]] = [("inherited", None, 128)]
    if "minimal" in capability.supported_efforts:
        attempts.append(("minimal", "minimal", 2048))
    if capability.can_disable:
        attempts.append(("none", "none", 128))
    return attempts


async def _run_attempt(
    model: LiveModel,
    *,
    label: str,
    effort: str | None,
    max_tokens: int,
    timeout_seconds: float,
    prompt: str,
) -> dict[str, Any]:
    client = create_llm_client(
        model.provider,
        model.api_key,
        model.model,
        model.base_url,
        timeout=min(timeout_seconds, 120.0),
    )
    started = time.monotonic()
    first_event_ms: int | None = None
    first_output_ms: int | None = None

    async def on_thinking(text: str) -> None:
        nonlocal first_event_ms
        if text and first_event_ms is None:
            first_event_ms = int((time.monotonic() - started) * 1000)

    async def on_chunk(text: str) -> None:
        nonlocal first_event_ms, first_output_ms
        if not text:
            return
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if first_event_ms is None:
            first_event_ms = elapsed_ms
        if first_output_ms is None:
            first_output_ms = elapsed_ms

    try:
        response = await asyncio.wait_for(
            client.stream(
                [LLMMessage(role="user", content=prompt)],
                temperature=None,
                max_tokens=max_tokens,
                reasoning_effort=effort,
                on_chunk=on_chunk,
                on_thinking=on_thinking,
            ),
            timeout=timeout_seconds,
        )
        content = str(response.content or "").strip()
        if not content:
            raise RuntimeError("provider returned an empty response")
        return {
            "attempt": label,
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "first_event_ms": first_event_ms,
            "first_output_ms": first_output_ms,
            "reasoning_chars": len(response.reasoning_content or ""),
            "output_chars": len(content),
            "usage": response.usage or {},
        }
    except Exception as exc:
        return {
            "attempt": label,
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    finally:
        await client.close()


async def _run_model(
    model: LiveModel,
    timeout_seconds: float,
    *,
    all_efforts: bool,
    matrix_max_tokens: int,
    prompt: str,
) -> dict[str, Any]:
    capability = resolve_reasoning_capability(
        provider=model.provider,
        model=model.model,
        base_url=model.base_url,
    )
    results = []
    for label, effort, max_tokens in _attempts(
        model,
        all_efforts=all_efforts,
        matrix_max_tokens=matrix_max_tokens,
    ):
        results.append(
            await _run_attempt(
                model,
                label=label,
                effort=effort,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                prompt=prompt,
            )
        )
    return {
        "provider": model.provider,
        "model": model.model,
        "label": model.label,
        "profile": capability.profile,
        "supported_efforts": list(capability.supported_efforts),
        "ok": all(item["ok"] for item in results),
        "attempts": results,
    }


async def _run_all(
    models: list[LiveModel],
    *,
    concurrency: int,
    timeout_seconds: float,
    all_efforts: bool,
    matrix_max_tokens: int,
    prompt: str,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(model: LiveModel) -> dict[str, Any]:
        async with semaphore:
            result = await _run_model(
                model,
                timeout_seconds,
                all_efforts=all_efforts,
                matrix_max_tokens=matrix_max_tokens,
                prompt=prompt,
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result

    return await asyncio.gather(*(bounded(model) for model in models))


def _load_inventory() -> list[LiveModel]:
    raw = json.load(sys.stdin)
    if not isinstance(raw, list) or not raw:
        raise ValueError("stdin must contain a non-empty JSON model list")
    models = [LiveModel.from_dict(item) for item in raw]
    identities = {(item.provider, item.model, item.base_url) for item in models}
    if len(identities) != len(models):
        raise ValueError("model inventory contains duplicate runtime identities")
    return models


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--all-efforts", action="store_true")
    parser.add_argument("--matrix-max-tokens", type=int, default=65_536)
    parser.add_argument(
        "--prompt",
        default="Reply with exactly OK.",
        help="Fixed prompt used for every model and effort.",
    )
    args = parser.parse_args()
    models = _load_inventory()
    if len(models) != args.expected_count:
        raise ValueError(
            f"inventory count {len(models)} does not match expected {args.expected_count}"
        )
    results = asyncio.run(
        _run_all(
            models,
            concurrency=max(1, args.concurrency),
            timeout_seconds=args.timeout_seconds,
            all_efforts=args.all_efforts,
            matrix_max_tokens=max(2, args.matrix_max_tokens),
            prompt=args.prompt,
        )
    )
    passed = sum(1 for item in results if item["ok"])
    summary = {"models": len(results), "passed": passed, "failed": len(results) - passed}
    print(json.dumps({"summary": summary}, ensure_ascii=False), flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
