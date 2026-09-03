"""Bounded read client for a standard SCIM 2.0 directory."""

import asyncio
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.services.scim_directory import (
    SCIM_ENTERPRISE_USER_SCHEMA,
    SCIM_GROUP_SCHEMA,
    SCIM_USER_SCHEMA,
    ScimDirectorySnapshot,
    ScimSnapshotError,
)


class ScimClient:
    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        page_size: int = 500,
        max_resources: int = 200_000,
        timeout_seconds: float = 30,
        max_retries: int = 3,
        max_requests: int = 2_000,
        user_field_mapping: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("SCIM base_url is required")
        if not client_id or not client_secret:
            raise ValueError("SCIM client credentials are required")
        self.base_url = base_url.rstrip("/")
        self.page_size = max(1, min(page_size, 1_000))
        self.max_resources = max(1, max_resources)
        self.max_retries = max(0, min(max_retries, 5))
        self.max_requests = max(3, max_requests)
        self._request_count = 0
        self.user_field_mapping = user_field_mapping
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(client_id, client_secret),
            headers={"Accept": "application/scim+json, application/json"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    @classmethod
    def from_provider_config(cls, config: dict[str, Any]) -> "ScimClient":
        directory = config.get("directory") or {}
        return cls(
            base_url=directory.get("base_url") or config.get("scim_base_url") or "",
            client_id=config.get("client_id") or config.get("app_id") or "",
            client_secret=config.get("client_secret") or config.get("app_secret") or "",
            page_size=int(directory.get("page_size") or config.get("scim_page_size") or 500),
            max_resources=int(directory.get("max_resources") or 200_000),
            user_field_mapping=directory.get("field_mapping"),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def discover_capabilities(self) -> dict[str, Any]:
        service = await self._get("/ServiceProviderConfig")
        resource_types = await self._get("/ResourceTypes")
        schemas = await self._get("/Schemas")
        capabilities = {
            "patch_supported": bool((service.get("patch") or {}).get("supported")),
            "filter_supported": bool((service.get("filter") or {}).get("supported")),
            "resource_types": tuple(
                str(item.get("name") or item.get("id") or "")
                for item in resource_types.get("Resources") or []
            ),
            "schemas": tuple(
                str(item.get("id") or "") for item in schemas.get("Resources") or []
            ),
        }
        resource_types_found = set(capabilities["resource_types"])
        schemas_found = set(capabilities["schemas"])
        if not {"User", "Group"}.issubset(resource_types_found):
            raise ScimSnapshotError("SCIM discovery does not expose User and Group resources")
        if not {SCIM_USER_SCHEMA, SCIM_GROUP_SCHEMA}.issubset(schemas_found):
            raise ScimSnapshotError("SCIM discovery does not expose core User and Group schemas")
        return capabilities

    async def discover_user_fields(
        self,
        target_account: str | None = None,
    ) -> tuple[dict[str, str], ...]:
        """Return SCIM User paths with one bounded real sample value."""
        schemas = await self._get("/Schemas")
        normalized_target = str(target_account or "").strip()
        sample = await self._discovery_sample(normalized_target)
        paths: set[str] = set()
        for schema in schemas.get("Resources") or []:
            if not isinstance(schema, dict):
                continue
            schema_id = str(schema.get("id") or "")
            if schema_id not in {SCIM_USER_SCHEMA, SCIM_ENTERPRISE_USER_SCHEMA}:
                continue
            prefix = "" if schema_id == SCIM_USER_SCHEMA else f"/{schema_id}"
            _collect_schema_paths(schema.get("attributes"), prefix, paths)
        resources = sample.get("Resources") or []
        if normalized_target and not resources:
            raise ScimSnapshotError("SCIM target account was not found")
        if normalized_target and len(resources) > 1:
            raise ScimSnapshotError("SCIM target account is not unique")
        sample_values: dict[str, str] = {}
        if isinstance(resources, list) and resources and isinstance(resources[0], dict):
            _collect_resource_fields(resources[0], "", paths, sample_values)
        paths = {
            path for path in paths
            if not _is_hidden_discovery_path(path)
        }
        return tuple(
            {"path": path, "sample_value": sample_values.get(path, "")}
            for path in sorted(paths)
        )

    async def _discovery_sample(self, target_account: str) -> dict[str, Any]:
        if not target_account:
            return await self._get(
                "/Users", params={"startIndex": 1, "count": 1}
            )
        escaped = target_account.replace("\\", "\\\\").replace('"', '\\"')
        attributes = (
            ("emails.value", "userName", "id")
            if "@" in target_account
            else ("userName", "id", "emails.value")
        )
        for attribute in attributes:
            try:
                result = await self._get(
                    "/Users",
                    params={
                        "startIndex": 1,
                        "count": 2,
                        "filter": f'{attribute} eq "{escaped}"',
                    },
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {400, 404, 422}:
                    continue
                raise
            resources = result.get("Resources") or []
            if len(resources) > 1:
                raise ScimSnapshotError("SCIM target account is not unique")
            if resources:
                return result
        return await self._scan_discovery_sample(target_account)

    async def _scan_discovery_sample(self, target_account: str) -> dict[str, Any]:
        """Find one exact account when a SCIM server declares no filter support."""
        start_index = 1
        inspected = 0
        while inspected < self.max_resources:
            page = await self._get(
                "/Users",
                params={"startIndex": start_index, "count": self.page_size},
            )
            resources = page.get("Resources")
            if not isinstance(resources, list):
                raise ScimSnapshotError("SCIM Users response has no Resources list")
            matches = [
                resource for resource in resources
                if isinstance(resource, dict)
                and _resource_matches_target(resource, target_account)
            ]
            if len(matches) > 1:
                raise ScimSnapshotError("SCIM target account is not unique")
            if matches:
                return {"totalResults": 1, "Resources": matches}
            inspected += len(resources)
            total = page.get("totalResults")
            if not resources or not isinstance(total, int) or inspected >= total:
                break
            start_index += len(resources)
        raise ScimSnapshotError("SCIM target account was not found")

    async def discover_user_field_paths(self) -> tuple[str, ...]:
        """Compatibility view of the discovered SCIM User paths."""
        return tuple(field["path"] for field in await self.discover_user_fields())

    async def fetch_snapshot(self) -> ScimDirectorySnapshot:
        await self.discover_capabilities()
        users = await self._list_all("/Users")
        groups = await self._list_all("/Groups")
        return ScimDirectorySnapshot.from_resources(
            users,
            groups,
            user_field_mapping=self.user_field_mapping,
        )

    async def _list_all(self, path: str) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        start_index = 1
        expected_total: int | None = None
        while expected_total is None or len(resources) < expected_total:
            page = await self._get(
                path,
                params={"startIndex": start_index, "count": self.page_size},
            )
            page_resources = page.get("Resources")
            if not isinstance(page_resources, list):
                raise ScimSnapshotError(f"{path} response has no Resources list")
            total = page.get("totalResults")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ScimSnapshotError(f"{path} response has invalid totalResults")
            if expected_total is None:
                expected_total = total
                if expected_total > self.max_resources:
                    raise ScimSnapshotError(f"{path} exceeds configured resource limit")
            elif total != expected_total:
                raise ScimSnapshotError(f"{path} totalResults changed during pagination")
            if not page_resources and len(resources) < expected_total:
                raise ScimSnapshotError(f"{path} pagination ended before totalResults")
            resources.extend(page_resources)
            if len(resources) > expected_total:
                raise ScimSnapshotError(f"{path} returned more resources than totalResults")
            start_index += len(page_resources)
        if len(resources) != (expected_total or 0):
            raise ScimSnapshotError(f"{path} pagination is incomplete")
        return resources

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        response = None
        for attempt in range(self.max_retries + 1):
            self._request_count += 1
            if self._request_count > self.max_requests:
                raise ScimSnapshotError("SCIM request budget exceeded")
            response = await self._client.get(f"{self.base_url}{path}", params=params)
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt == self.max_retries:
                break
            await asyncio.sleep(_retry_delay(response.headers.get("Retry-After"), attempt))
        assert response is not None
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ScimSnapshotError(f"{path} response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ScimSnapshotError(f"{path} response is not an object")
        return payload


def _retry_delay(value: str | None, attempt: int) -> float:
    """Honor bounded Retry-After values without allowing unbounded worker stalls."""
    if value:
        try:
            return max(0.0, min(float(value), 10.0))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return max(0.0, min((target - datetime.now(timezone.utc)).total_seconds(), 10.0))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(0.25 * (2**attempt), 2.0)


def _collect_schema_paths(attributes: Any, prefix: str, paths: set[str]) -> None:
    if not isinstance(attributes, list):
        return
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        name = str(attribute.get("name") or "").strip()
        if not name:
            continue
        path = f"{prefix}/{name}"
        paths.add(path)
        if not attribute.get("multiValued"):
            _collect_schema_paths(attribute.get("subAttributes"), path, paths)


def _collect_resource_fields(
    value: Any,
    prefix: str,
    paths: set[str],
    sample_values: dict[str, str],
) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        path = f"{prefix}/{key}"
        paths.add(path)
        sample_values[path] = _format_sample_value(item)
        if isinstance(item, dict):
            _collect_resource_fields(item, path, paths, sample_values)


def _format_sample_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        rendered = value
    elif isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        rendered = str(value)
    return rendered if len(rendered) <= 240 else f"{rendered[:237]}..."


def _resource_matches_target(resource: dict[str, Any], target: str) -> bool:
    if str(resource.get("id") or "") == target:
        return True
    normalized_target = target.casefold()
    if str(resource.get("userName") or "").casefold() == normalized_target:
        return True
    emails = resource.get("emails") or []
    return any(
        isinstance(email, dict)
        and str(email.get("value") or "").casefold() == normalized_target
        for email in emails
    )


def _is_hidden_discovery_path(path: str) -> bool:
    lowered = path.casefold()
    hidden_segments = {"password", "secret", "token", "authorization"}
    if any(segment in hidden_segments for segment in lowered.split("/")):
        return True
    return lowered in {"/schemas", "/id", "/active"} or lowered.startswith("/meta")
