"""Release imports use real tenant APIs and survive a lost create response."""

import json
import os
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

from app.database import engine
from app.main import app
from app.scripts.import_release_models import ModelImporter
from tests.test_enterprise_llm_tenant_scope import _seed_admin, _seed_tenant

pytestmark = pytest.mark.asyncio
BAILIAN = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TOKENHUB = "https://tokenhub.tencentmaas.com/v1"


@pytest.fixture(autouse=True)
async def connections():
    await engine.dispose()
    yield
    await engine.dispose()


def definition(model, platform="bailian", **overrides):
    return {"provider": "qwen" if platform == "bailian" else "tokenhub", "service_platform": platform,
            "model": model, "label": model, "api_protocol": "openai_compatible", "purposes": ["media_understanding"],
            "input_modalities": ["text", "image", "video"], "enabled": True, "context_window": 32000,
            "max_output_tokens": 4096, **overrides}


async def fixture_client():
    tenant = await _seed_tenant("ReleaseImport")
    token = await _seed_admin(tenant.id)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                              headers={"Authorization": "Bearer " + token})
    async def create(model, provider="qwen", **extra):
        response = await client.post("/api/enterprise/llm-models", json={
            "provider": provider, "model": model, "label": "Keep custom " + model,
            "api_key": "target-bailian-key" if provider == "qwen" else "target-tokenhub-key",
            "base_url": BAILIAN if provider == "qwen" else TOKENHUB, "api_protocol": "openai_responses",
            "purposes": ["conversation", "media_understanding"], "input_modalities": ["text", "image"],
            "context_window": 40000, "max_output_tokens": 4096, "temperature": 1.2,
            "extra_headers": {"X-Custom-Secret": "header-must-stay-private"}, **extra})
        assert response.status_code == 201, response.text
        return response.json()
    plus = await create("qwen3.5-plus")
    image = await create("qwen-image-2.0", purposes=["image_generation"], input_modalities=["text", "image"])
    await create("mimo-v2.5-pro", enabled=False, purposes=["conversation"], input_modalities=["text"])
    speech = await create("preserved-asr", purposes=["speech_recognition"], input_modalities=["audio"])
    response = await client.put("/api/enterprise/media-model-defaults", json={
        "understanding_model_id": plus["id"], "image_model_id": image["id"], "speech_model_id": speech["id"]})
    assert response.status_code == 200, response.text
    bindings = {"bailian": {"base_url": BAILIAN, "source_model_id": image["id"], "api_key": "target-bailian-key"},
                "tokenhub": {"base_url": TOKENHUB, "api_key": "target-tokenhub-key"}}
    baseline = await client.get("/api/enterprise/llm-models")
    return client, tenant, bindings, baseline.json()


async def test_plan_first_apply_and_repeat_preserve_target_configuration():
    client, tenant, bindings, original = await fixture_client()
    events = []
    async with aclosing(client):
        manifest_path = os.environ.get("MODEL_IMPORT_MANIFEST")
        manifest = json.loads(Path(manifest_path).read_text()) if manifest_path else {"models": [
            definition("qwen3.5-plus", api_protocol="openai_responses", purposes=["conversation", "media_understanding"]),
            definition("xiaomi/mimo-v2.5-pro", purposes=["conversation"], input_modalities=["text"]),
            definition("new-vision"), definition("hy-image-v3", "tokenhub", purposes=["image_generation"], enabled=False)]}
        importer = ModelImporter(client, str(tenant.id), bindings, emit=events.append)
        plan = await importer.run(manifest)
        assert plan["ok"] and not plan["applied"]
        assert len(await importer.models()) == len(original)
        first = await importer.run(manifest, apply=True)
        assert first["ok"] and first["defaults_preserved"]
        rows = await importer.models()
        after = {row["model"]: row for row in rows}
        assert "video" in after["qwen3.5-plus"]["input_modalities"]
        assert after["xiaomi/mimo-v2.5-pro"]["enabled"]
        assert after["xiaomi/mimo-v2.5-pro"]["effective_api_protocol"] == "openai_compatible"
        for old in original:
            current = after["xiaomi/mimo-v2.5-pro" if old["model"] == "mimo-v2.5-pro" else old["model"]]
            for field in ("id", "label", "extra_headers", "temperature", "context_window", "max_output_tokens", "api_key_masked"):
                assert current[field] == old[field]
        assert after["preserved-asr"]["purposes"] == ["speech_recognition"]
        again = await importer.run(manifest, apply=True)
        assert again["ok"] and again["counts"] == {"preserve": len(manifest["models"])}
        assert len(await importer.models()) == len(rows)
        log = "\n".join(events)
        assert "target-bailian-key" not in log and "header-must-stay-private" not in log
        foreign = await _seed_tenant("ReleaseForeign")
        denied = await client.get("/api/enterprise/llm-models", params={"tenant_id": str(foreign.id)})
        assert denied.status_code == 403
        print(json.dumps({"drill": "first_and_repeat", "manifest_models": len(manifest["models"]),
                          "first": first, "repeat": again}))


async def test_duplicate_and_other_endpoint_conflicts_do_not_mutate():
    client, tenant, bindings, original = await fixture_client()
    async with aclosing(client):
        duplicate = await client.post("/api/enterprise/llm-models", json={
            "provider": "qwen", "model": "qwen3.5-plus", "label": "Duplicate", "api_key": "target-bailian-key",
            "base_url": BAILIAN, "max_output_tokens": 4096})
        assert duplicate.status_code == 201
        other = await client.post("/api/enterprise/llm-models", json={
            "provider": "qwen", "model": "other-region-model", "label": "Other region", "api_key": "other-region-key",
            "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"})
        assert other.status_code == 201
        events = []
        importer = ModelImporter(client, str(tenant.id), bindings, emit=events.append)
        count = len(await importer.models())
        outcome = await importer.run({"models": [definition("not-created"), definition("qwen3.5-plus"),
                                                 definition("other-region-model")]}, apply=True)
        assert not outcome["ok"] and outcome["counts"]["conflict"] == 2
        assert len(await importer.models()) == count
        log = "\n".join(events)
        assert "duplicate_exact_target" in log and "other_endpoint_requires_review" in log
        assert "not-created" not in {row["model"] for row in await importer.models()}


async def test_lost_post_response_resumes_without_duplicate():
    client, tenant, bindings, original = await fixture_client()
    class LoseCreateResponse(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=app)
            self.lost = False

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.method == "POST" and request.url.path.endswith("/llm-models") and not self.lost:
                self.lost = True
                await response.aread()
                await response.aclose()
                return httpx.Response(503, json={"detail": "response lost after commit"}, request=request)
            return response

    manifest = {"models": [definition("response-lost-model"), definition("remaining-model")]}
    async with aclosing(client):
        async with httpx.AsyncClient(transport=LoseCreateResponse(), base_url="http://test", headers=client.headers) as lossy:
            failed = await ModelImporter(lossy, str(tenant.id), bindings, emit=lambda _: None).run(manifest, apply=True)
            assert not failed["ok"]
        importer = ModelImporter(client, str(tenant.id), bindings, emit=lambda _: None)
        resumed = await importer.run(manifest, apply=True)
        assert resumed["ok"] and resumed["counts"] == {"preserve": 1, "create": 1}
        rows = await importer.models()
        assert sum(row["model"] == "response-lost-model" for row in rows) == 1
