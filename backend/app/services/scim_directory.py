"""Canonical SCIM User/Group snapshot shared by every directory adapter."""

from dataclasses import dataclass, field
from typing import Any

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_ENTERPRISE_USER_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
SCIM_ENTERPRISE_USER_PATH = f"{SCIM_ENTERPRISE_USER_SCHEMA}/"
DEFAULT_SCIM_USER_FIELD_MAPPING = {
    "name": "/displayName",
    "email": "/emails",
    "mobile": "/phoneNumbers",
    "avatar": "/photos",
    "title": "/title",
    "employee_number": f"/{SCIM_ENTERPRISE_USER_PATH}employeeNumber",
    "organization": f"/{SCIM_ENTERPRISE_USER_PATH}organization",
    "division": f"/{SCIM_ENTERPRISE_USER_PATH}division",
    "department": f"/{SCIM_ENTERPRISE_USER_PATH}department",
}


class ScimSnapshotError(ValueError):
    """A remote snapshot is incomplete or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ScimMemberRef:
    value: str
    resource_type: str


@dataclass(frozen=True, slots=True)
class ScimUser:
    id: str
    user_name: str
    display_name: str
    active: bool
    emails: tuple[str, ...] = ()
    phone_numbers: tuple[str, ...] = ()
    photos: tuple[str, ...] = ()
    title: str | None = None
    employee_number: str | None = None
    organization: str | None = None
    division: str | None = None
    department: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_resource(
        cls,
        resource: dict[str, Any],
        field_mapping: dict[str, str] | None = None,
    ) -> "ScimUser":
        _require_schema(resource, SCIM_USER_SCHEMA, "User")
        external_id = _required_string(resource, "id", "User")
        enterprise = resource.get(SCIM_ENTERPRISE_USER_SCHEMA)
        if enterprise is not None and not isinstance(enterprise, dict):
            raise ScimSnapshotError(f"User {external_id} has an invalid enterprise extension")
        mapping = normalize_scim_user_field_mapping(field_mapping)
        name = _mapped_string(resource, mapping["name"])
        return cls(
            id=external_id,
            user_name=str(resource.get("userName") or "").strip(),
            display_name=name or str(resource.get("userName") or external_id).strip(),
            active=_boolean(resource.get("active", True), f"User {external_id} active"),
            emails=_mapped_values(resource, mapping["email"]),
            phone_numbers=_mapped_values(resource, mapping["mobile"]),
            photos=_mapped_values(resource, mapping["avatar"]),
            title=_optional_string(_resolve_attribute_path(resource, mapping["title"])),
            employee_number=_optional_string(
                _resolve_attribute_path(resource, mapping["employee_number"])
            ),
            organization=_optional_string(
                _resolve_attribute_path(resource, mapping["organization"])
            ),
            division=_optional_string(_resolve_attribute_path(resource, mapping["division"])),
            department=_optional_string(
                _resolve_attribute_path(resource, mapping["department"])
            ),
            raw=resource,
        )

    @property
    def organizational_path(self) -> str:
        """Return the SCIM enterprise display hierarchy without inventing Group IDs."""
        parts: list[str] = []
        for value in (self.organization, self.division, self.department):
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ScimGroup:
    id: str
    display_name: str
    members: tuple[ScimMemberRef, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "ScimGroup":
        _require_schema(resource, SCIM_GROUP_SCHEMA, "Group")
        external_id = _required_string(resource, "id", "Group")
        refs: list[ScimMemberRef] = []
        members = resource.get("members") or []
        if not isinstance(members, list):
            raise ScimSnapshotError(f"Group {external_id} members is not a list")
        for member in members:
            if not isinstance(member, dict):
                raise ScimSnapshotError(f"Group {external_id} has an invalid member")
            value = _required_string(member, "value", f"Group {external_id} member")
            resource_type = str(member.get("type") or "").strip().title()
            if resource_type not in {"User", "Group"}:
                ref = str(member.get("$ref") or "")
                resource_type = "Group" if "/Groups/" in ref else "User" if "/Users/" in ref else ""
            if resource_type not in {"User", "Group"}:
                raise ScimSnapshotError(f"Group {external_id} member {value} has no supported type")
            refs.append(ScimMemberRef(value=value, resource_type=resource_type))
        return cls(
            id=external_id,
            display_name=str(resource.get("displayName") or external_id).strip(),
            members=tuple(refs),
            raw=resource,
        )


@dataclass(frozen=True, slots=True)
class ScimDirectorySnapshot:
    users: tuple[ScimUser, ...]
    groups: tuple[ScimGroup, ...]

    @classmethod
    def from_resources(
        cls,
        user_resources: list[dict[str, Any]],
        group_resources: list[dict[str, Any]],
        user_field_mapping: dict[str, str] | None = None,
    ) -> "ScimDirectorySnapshot":
        if not all(isinstance(item, dict) for item in user_resources):
            raise ScimSnapshotError("Users contains a non-object resource")
        if not all(isinstance(item, dict) for item in group_resources):
            raise ScimSnapshotError("Groups contains a non-object resource")
        snapshot = cls(
            users=tuple(
                ScimUser.from_resource(item, user_field_mapping) for item in user_resources
            ),
            groups=tuple(ScimGroup.from_resource(item) for item in group_resources),
        )
        snapshot.validate()
        return snapshot

    def validate(self) -> None:
        user_ids = _unique_ids("User", self.users)
        group_ids = _unique_ids("Group", self.groups)
        edges: dict[str, set[str]] = {group_id: set() for group_id in group_ids}
        for group in self.groups:
            seen_refs: set[tuple[str, str]] = set()
            for member in group.members:
                key = (member.resource_type, member.value)
                if key in seen_refs:
                    raise ScimSnapshotError(
                        f"Group {group.id} repeats {member.resource_type} member {member.value}"
                    )
                seen_refs.add(key)
                targets = user_ids if member.resource_type == "User" else group_ids
                if member.value not in targets:
                    raise ScimSnapshotError(
                        f"Group {group.id} references missing {member.resource_type} {member.value}"
                    )
                if member.resource_type == "Group":
                    edges[group.id].add(member.value)
        _assert_acyclic(edges)

    def group_edges(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (group.id, member.value)
            for group in self.groups
            for member in group.members
            if member.resource_type == "Group"
        )

    def user_groups(self) -> dict[str, tuple[str, ...]]:
        memberships: dict[str, list[str]] = {user.id: [] for user in self.users}
        for group in self.groups:
            for member in group.members:
                if member.resource_type == "User":
                    memberships[member.value].append(group.id)
        return {user_id: tuple(sorted(group_ids)) for user_id, group_ids in memberships.items()}

    def topological_groups(self) -> tuple[ScimGroup, ...]:
        """Return parents before children with stable provider-ID ordering."""
        by_id = {group.id: group for group in self.groups}
        children = {group_id: set() for group_id in by_id}
        indegree = {group_id: 0 for group_id in by_id}
        for parent_id, child_id in self.group_edges():
            children[parent_id].add(child_id)
            indegree[child_id] += 1
        ready = sorted(group_id for group_id, degree in indegree.items() if degree == 0)
        ordered: list[ScimGroup] = []
        while ready:
            group_id = ready.pop(0)
            ordered.append(by_id[group_id])
            for child_id in sorted(children[group_id]):
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)
                    ready.sort()
        return tuple(ordered)


def primary_value(items: Any) -> str:
    """Choose an explicitly primary SCIM multivalue, otherwise the first value."""
    candidates = _valid_multi_values(items)
    primary = next((item for item in candidates if item.get("primary") is True), None)
    chosen = primary or (candidates[0] if candidates else None)
    return str(chosen.get("value") or "").strip() if chosen else ""


def normalize_scim_user_field_mapping(
    field_mapping: Any,
) -> dict[str, str]:
    """Merge configured User attribute paths over the SCIM defaults."""
    if field_mapping is None:
        return dict(DEFAULT_SCIM_USER_FIELD_MAPPING)
    if not isinstance(field_mapping, dict):
        raise ScimSnapshotError("SCIM directory field_mapping must be an object")
    unknown = set(field_mapping) - set(DEFAULT_SCIM_USER_FIELD_MAPPING)
    if unknown:
        raise ScimSnapshotError("SCIM directory field_mapping has unsupported fields")
    normalized = dict(DEFAULT_SCIM_USER_FIELD_MAPPING)
    for key, value in field_mapping.items():
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or len(value) > 512
        ):
            raise ScimSnapshotError(
                "SCIM directory field_mapping paths must be JSON Pointers"
            )
        normalized[key] = value.strip()
    return normalized


def _resolve_attribute_path(resource: dict[str, Any], path: str) -> Any:
    """Resolve slash-separated SCIM attribute paths, preserving URN keys."""
    if path in resource:
        return resource[path]
    segments = [segment for segment in path.strip("/").split("/") if segment]
    current: Any = resource
    for segment in segments:
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def _mapped_values(resource: dict[str, Any], path: str) -> tuple[str, ...]:
    value = _resolve_attribute_path(resource, path)
    if isinstance(value, list):
        candidates = _valid_multi_values(value)
        candidates.sort(key=lambda item: item.get("primary") is not True)
        return tuple(str(item.get("value") or "").strip() for item in candidates)
    if path in {"/emails", "/phoneNumbers"} and value is not None:
        _valid_multi_values(value)
    scalar = _optional_string(value)
    return (scalar,) if scalar else ()


def _mapped_string(resource: dict[str, Any], path: str) -> str:
    value = _resolve_attribute_path(resource, path)
    if isinstance(value, list):
        return primary_value(value)
    return str(value or "").strip()


def _multi_values(items: Any) -> tuple[str, ...]:
    return tuple(str(item.get("value") or "").strip() for item in _valid_multi_values(items))


def _valid_multi_values(items: Any) -> list[dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ScimSnapshotError("SCIM multi-valued attribute is not a list of objects")
    return [item for item in items if str(item.get("value") or "").strip()]


def _boolean(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ScimSnapshotError(f"{label} is not a boolean")


def _require_schema(resource: dict[str, Any], schema: str, label: str) -> None:
    schemas = resource.get("schemas")
    if not isinstance(schemas, list) or schema not in schemas:
        raise ScimSnapshotError(f"{label} is missing required schema {schema}")


def _required_string(resource: dict[str, Any], key: str, label: str) -> str:
    value = str(resource.get(key) or "").strip()
    if not value:
        raise ScimSnapshotError(f"{label} is missing {key}")
    return value


def _optional_string(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _unique_ids(label: str, resources: tuple[Any, ...]) -> set[str]:
    ids = [resource.id for resource in resources]
    if len(ids) != len(set(ids)):
        raise ScimSnapshotError(f"{label} IDs are not unique")
    return set(ids)


def _assert_acyclic(edges: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(group_id: str) -> None:
        if group_id in visiting:
            raise ScimSnapshotError(f"Group membership cycle includes {group_id}")
        if group_id in visited:
            return
        visiting.add(group_id)
        for child_id in edges[group_id]:
            visit(child_id)
        visiting.remove(group_id)
        visited.add(group_id)

    for group_id in edges:
        visit(group_id)
