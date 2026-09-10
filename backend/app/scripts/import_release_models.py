"""One-shot enterprise model import; never submits a provider inference request.

Run plan/apply in Docker. Standard input is a private JSON object containing
access_token (or access_token_env) and connections keyed by serving platform:
{base_url, optional source_model_id, api_key or api_key_env}. Credentials stay in memory.
The manifest contains only non-secret model definitions; API IDs come from the
selected target tenant. Run one operator at a time and reread after interruption.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.services.llm.client_registry import get_provider_spec
from app.services.model_platform import model_service_platform

ALIASES = {("bailian", "xiaomi/mimo-v2.5-pro"): "mimo-v2.5-pro"}
ENABLE_EXISTING = {("bailian", "xiaomi/mimo-v2.5-pro")}
CREATE_FIELDS = {
    "provider", "model", "label", "api_protocol", "purposes", "input_modalities",
    "enabled", "context_window", "max_output_tokens", "context_usage_ratio",
    "temperature", "reasoning_effort", "request_timeout", "keep_recent_turns",
}
SAFE_FIELDS = CREATE_FIELDS | {"id", "service_platform", "effective_api_protocol", "supports_vision"}
PRESERVE_FIELDS = {
    "id", "label", "provider", "base_url", "api_key_masked", "extra_headers",
    "temperature", "reasoning_effort", "max_tokens_per_day", "max_output_tokens",
    "request_timeout", "context_window", "context_usage_ratio", "keep_recent_turns",
}


class ImportFailure(RuntimeError):
    pass


def endpoint(value: str | None, provider: str = "") -> str:
    if not value:
        spec = get_provider_spec(provider)
        value = spec.default_base_url if spec else None
    parts = urlsplit(value or "")
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ImportFailure("Endpoint must be an approved HTTP(S) base URL without credentials/query/fragment")
    host = parts.hostname.lower()
    port = parts.port
    authority = f"[{host}]" if ":" in host else host
    if port and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
        authority += f":{port}"
    return urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))


def read_manifest(data: dict) -> list[dict]:
    rows = data.get("models")
    if not isinstance(rows, list) or not rows:
        raise ImportFailure("Manifest must contain a nonempty models list")
    seen = set()
    for row in rows:
        if set(row) - CREATE_FIELDS - {"service_platform"}:
            raise ImportFailure("Manifest includes an unexpected field; credentials and source IDs belong only in private stdin")
        key = row["service_platform"], row["model"]
        if key in seen:
            raise ImportFailure(f"Duplicate manifest model: {key[0]} / {key[1]}")
        seen.add(key)
    return rows


def safe(row: dict | None) -> dict | None:
    return {key: row[key] for key in sorted(SAFE_FIELDS) if key in row} if row else None


def platform(row: dict) -> str:
    return row.get("service_platform") or model_service_platform(row["provider"], row.get("base_url"))


def matching(rows: list[dict], desired: dict, approved_endpoint: str) -> tuple[dict | None, str | None]:
    names = {desired["model"], ALIASES.get((desired["service_platform"], desired["model"]))}
    same = [row for row in rows if platform(row) == desired["service_platform"] and row["model"] in names]
    exact = [row for row in same if endpoint(row.get("base_url"), row["provider"]) == approved_endpoint]
    if len(exact) > 1:
        return None, "duplicate_exact_target"
    if same and not exact:
        return None, "same_model_at_other_endpoint_requires_review"
    return (exact[0] if exact else None), None


def changes(current: dict, desired: dict) -> dict:
    update = {}
    if current["model"] != desired["model"]:
        update["model"] = desired["model"]
    for field in ("purposes", "input_modalities"):
        merged = list(dict.fromkeys([*current.get(field, []), *desired[field]]))
        if set(merged) != set(current.get(field, [])):
            update[field] = merged
    effective = current.get("effective_api_protocol") or current.get("api_protocol")
    if effective != desired["api_protocol"]:
        update["api_protocol"] = desired["api_protocol"]
    if "image" in update.get("input_modalities", current.get("input_modalities", [])) and not current.get("supports_vision"):
        update["supports_vision"] = True
    if (desired["service_platform"], desired["model"]) in ENABLE_EXISTING and not current["enabled"]:
        update["enabled"] = True
    return update


class ModelImporter:
    def __init__(self, client: httpx.AsyncClient, tenant_id: str, connections: dict, emit=None):
        self.client = client
        self.tenant_id = str(uuid.UUID(tenant_id))
        self.connections = connections
        sink = emit or (lambda line: print(line, flush=True))
        self.emit = lambda value: sink(json.dumps(value, ensure_ascii=False))
        self.params = {"tenant_id": self.tenant_id}

    async def api(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        if not response.is_success:
            # Do not echo provider headers, request bodies or arbitrary server text.
            raise ImportFailure(f"{method} {path}: HTTP {response.status_code}")
        return response.json()

    async def models(self):
        return await self.api("GET", "/api/enterprise/llm-models", params=self.params)

    async def defaults(self):
        tenant = await self.api("GET", f"/api/tenants/{self.tenant_id}")
        if str(tenant["id"]) != self.tenant_id:
            raise ImportFailure("API tenant identity mismatch")
        media = await self.api("GET", "/api/enterprise/media-model-defaults", params=self.params)
        return {"conversation": tenant.get("default_model_id"), "media": media}

    def binding(self, desired, rows, *, need_key=False):
        connection = self.connections.get(desired["service_platform"])
        if not isinstance(connection, dict):
            raise ImportFailure(f"Missing approved connection: {desired['service_platform']}")
        approved = endpoint(connection.get("base_url"))
        if model_service_platform(desired["provider"], approved) != desired["service_platform"]:
            raise ImportFailure("Approved endpoint does not match the manifest serving platform")
        key = connection.get("api_key") or os.environ.get(connection.get("api_key_env", ""))
        if need_key and not key:
            raise ImportFailure("Missing target provider key in private stdin/environment")
        # A new serving platform legitimately has no existing model. In that
        # case the operator supplies its credential explicitly through stdin/env.
        source_id = connection.get("source_model_id")
        if source_id:
            source = next((row for row in rows if row["id"] == source_id), None)
            if source is None or platform(source) != desired["service_platform"] or endpoint(source.get("base_url"), source["provider"]) != approved:
                raise ImportFailure("Credential source must be an existing target-tenant model on the approved platform and endpoint")
            if key and source.get("api_key_masked") and source["api_key_masked"] != "****" and not key.endswith(source["api_key_masked"].removeprefix("****")):
                raise ImportFailure("Provided credential does not match the target source model's masked suffix")
        return approved, key

    async def run(self, manifest: dict, *, apply=False) -> dict:
        desired_rows = read_manifest(manifest)
        baseline_defaults = await self.defaults()
        before = await self.models()
        actions = []
        for desired in desired_rows:
            approved, _ = self.binding(desired, before)
            current, conflict = matching(before, desired, approved)
            if current and not desired["enabled"] and current["enabled"]:
                conflict = "provider_blocked_manifest_but_existing_enabled_requires_review"
            update = changes(current, desired) if current else {}
            if not current and desired["enabled"] and "conversation" in desired["purposes"] and baseline_defaults["conversation"] is None:
                conflict = "creating_first_conversation_would_change_empty_default"
            action = ("update" if update else "preserve") if current else "create"
            if conflict:
                action = "conflict"
            item = {"platform": desired["service_platform"], "model": desired["model"], "action": action,
                    "endpoint_sha256": hashlib.sha256(approved.encode()).hexdigest(),
                    "before": safe(current), "after": safe({**current, **update}) if current else safe(desired),
                    "changes": update, "conflict": conflict}
            actions.append((desired, item))
            self.emit({"stage": "plan", **item})
        counts = Counter(item["action"] for _, item in actions)
        if counts["conflict"] or not apply:
            result = {"stage": "summary", "applied": False, "counts": dict(counts), "ok": not counts["conflict"]}
            self.emit(result)
            return result
        # Preflight every needed credential before the first write.
        for desired, item in actions:
            if item["action"] == "create":
                self.binding(desired, before, need_key=True)
        completed = []
        for desired, planned in actions:
            fresh = await self.models()
            approved, key = self.binding(desired, fresh, need_key=planned["action"] == "create")
            current, conflict = matching(fresh, desired, approved)
            if conflict or (planned["before"] is not None and (current is None or current["id"] != planned["before"]["id"])):
                raise ImportFailure("Target changed during import; rerun plan")
            update = changes(current, desired) if current else {}
            try:
                if current:
                    result = await self.api("PUT", f"/api/enterprise/llm-models/{current['id']}", json=update) if update else current
                    action = "updated" if update else "preserved"
                else:
                    payload = {field: value for field, value in desired.items() if field in CREATE_FIELDS}
                    result = await self.api("POST", "/api/enterprise/llm-models", params=self.params,
                                            json={**payload, "api_key": key, "base_url": approved})
                    action = "created"
                # GET populates api_key_masked; write responses intentionally do
                # not. Compare the same API projection before and after writes.
                reread, mismatch = matching(await self.models(), desired, approved)
                if mismatch or reread is None or reread["id"] != result["id"]:
                    raise ImportFailure("Written model identity differs on API reread")
                result = reread
                if current and any(result.get(field) != current.get(field) for field in PRESERVE_FIELDS):
                    raise ImportFailure("An existing custom setting changed unexpectedly")
            except (httpx.HTTPError, ImportFailure) as exc:
                # A POST may have committed even when its response was lost. Do
                # not replay it. The next invocation reads the pool before work.
                self.emit({"stage": "write_failed", "model": desired["model"], "platform": desired["service_platform"],
                           "error": str(exc) if isinstance(exc, ImportFailure) else type(exc).__name__,
                           "resume": "Reread with plan, then apply; no automatic write retry"})
                return {"ok": False, "applied": True, "completed": len(completed)}
            completed.append((desired, result))
            self.emit({"stage": "applied", "action": action, "model": desired["model"], "id": result["id"], "after": safe(result)})
        after = await self.models()
        for desired, saved in completed:
            approved, _ = self.binding(desired, after)
            current, conflict = matching(after, desired, approved)
            if conflict or current is None or current["id"] != saved["id"] or changes(current, desired):
                raise ImportFailure("Final API reread does not match the applied model")
            if any(current.get(field) != saved.get(field) for field in SAFE_FIELDS | PRESERVE_FIELDS):
                raise ImportFailure("A model changed before the final API reread")
        if await self.defaults() != baseline_defaults:
            raise ImportFailure("Default model references changed during import")
        result = {"stage": "summary", "ok": True, "applied": True, "models": len(completed),
                  "pool_before": len(before), "pool_after": len(after), "counts": dict(counts), "defaults_preserved": True}
        self.emit(result)
        return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["plan", "apply"])
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    private = json.load(sys.stdin)
    token = private.get("access_token") or os.environ.get(private.get("access_token_env", ""))
    if not token:
        raise ImportFailure("Missing administrator access token")
    manifest = json.loads(args.manifest.read_text())
    async with httpx.AsyncClient(base_url=endpoint(args.api_url), headers={"Authorization": "Bearer " + token},
                                 timeout=30, follow_redirects=False) as client:
        result = await ModelImporter(client, args.tenant_id, private["connections"]).run(manifest, apply=args.mode == "apply")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (ImportFailure, httpx.HTTPError) as exc:
        print(json.dumps({"stage": "failed", "error": str(exc) if isinstance(exc, ImportFailure) else type(exc).__name__}))
        raise SystemExit(1) from None
