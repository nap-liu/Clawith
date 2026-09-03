"""Convert vendor directory records into the canonical SCIM semantic graph."""

from app.services.org_sync_models import ExternalDepartment, ExternalUser
from app.services.scim_directory import (
    SCIM_GROUP_SCHEMA,
    SCIM_USER_SCHEMA,
    ScimDirectorySnapshot,
    ScimSnapshotError,
)


def build_vendor_scim_snapshot(
    departments: list[ExternalDepartment],
    users: list[ExternalUser],
) -> ScimDirectorySnapshot:
    """Validate every vendor snapshot with the same User/Group invariants as SCIM."""
    group_ids = {str(department.external_id).strip() for department in departments}
    if not group_ids:
        raise ScimSnapshotError("Vendor directory snapshot has no Groups")
    if "" in group_ids:
        raise ScimSnapshotError("Vendor Group is missing id")

    members_by_group: dict[str, list[dict[str, str]]] = {
        group_id: [] for group_id in group_ids
    }
    group_resources: list[dict] = []
    for department in departments:
        group_id = str(department.external_id).strip()
        parent_ids = dict.fromkeys(
            str(value).strip()
            for value in [
                getattr(department, "parent_external_id", None),
                *getattr(department, "parent_external_ids", []),
            ]
            if value is not None and str(value).strip()
        )
        for parent_id in parent_ids:
            if parent_id not in group_ids:
                raise ScimSnapshotError(
                    f"Group {group_id} references missing Group {parent_id}"
                )
            members_by_group[parent_id].append(
                {"value": group_id, "type": "Group"}
            )
        group_resources.append(
            {
                "schemas": [SCIM_GROUP_SCHEMA],
                "id": group_id,
                "displayName": department.name or group_id,
                "members": members_by_group[group_id],
            }
        )

    user_resources: list[dict] = []
    for user in users:
        user_id = str(user.external_id).strip()
        if not user_id:
            raise ScimSnapshotError("Vendor User is missing id")
        memberships = dict.fromkeys(
            str(value).strip()
            for value in [*user.department_ids, user.department_external_id]
            if value is not None and str(value).strip()
        )
        for group_id in memberships:
            if group_id not in group_ids:
                raise ScimSnapshotError(
                    f"User {user_id} references missing Group {group_id}"
                )
            members_by_group[group_id].append(
                {"value": user_id, "type": "User"}
            )
        user_resources.append(
            {
                "schemas": [SCIM_USER_SCHEMA],
                "id": user_id,
                "userName": user.email or user.mobile or user_id,
                "displayName": user.name or user_id,
                "active": user.status == "active",
                "emails": ([{"value": user.email, "primary": True}] if user.email else []),
                "phoneNumbers": (
                    [{"value": user.mobile, "primary": True}] if user.mobile else []
                ),
                "photos": (
                    [{"value": user.avatar_url, "primary": True}]
                    if user.avatar_url else []
                ),
            }
        )

    return ScimDirectorySnapshot.from_resources(user_resources, group_resources)
