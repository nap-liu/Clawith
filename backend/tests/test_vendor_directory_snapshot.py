import pytest

from app.services.org_sync_models import (
    PLATFORM_ENTERPRISE_ROOT_EXTERNAL_ID,
    ExternalDepartment,
    ExternalUser,
    normalize_enterprise_root,
)
from app.services.scim_directory import ScimSnapshotError
from app.services.vendor_directory_snapshot import build_vendor_scim_snapshot


def test_vendor_snapshot_normalizes_multi_parent_and_multi_group_membership():
    snapshot = build_vendor_scim_snapshot(
        [
            ExternalDepartment(external_id="root-a", name="Root A"),
            ExternalDepartment(external_id="root-b", name="Root B"),
            ExternalDepartment(
                external_id="team",
                name="Team",
                parent_external_id="root-a",
                parent_external_ids=["root-b"],
            ),
        ],
        [
            ExternalUser(
                external_id="person-1",
                name="Person",
                avatar_url="https://cdn.example/person.png",
                department_external_id="team",
                department_ids=["root-b", "team"],
            )
        ],
    )

    assert set(snapshot.group_edges()) == {("root-a", "team"), ("root-b", "team")}
    assert snapshot.user_groups()["person-1"] == ("root-b", "team")
    assert snapshot.users[0].photos == ("https://cdn.example/person.png",)


def test_enterprise_root_mapping_renames_one_structural_root():
    departments = [
        ExternalDepartment(external_id="1", name="Root"),
        ExternalDepartment(external_id="team", name="Team", parent_external_id="1"),
    ]

    normalized = normalize_enterprise_root(departments, root_name="Example Corp")

    assert normalized[0].external_id == "1"
    assert normalized[0].name == "Example Corp"
    assert build_vendor_scim_snapshot(normalized, []).group_edges() == (("1", "team"),)


def test_enterprise_root_mapping_wraps_multiple_provider_roots():
    departments = [
        ExternalDepartment(external_id="north", name="North"),
        ExternalDepartment(external_id="south", name="South"),
    ]

    normalized = normalize_enterprise_root(departments, root_name="Example Corp")
    snapshot = build_vendor_scim_snapshot(normalized, [])

    assert normalized[0].external_id == PLATFORM_ENTERPRISE_ROOT_EXTERNAL_ID
    assert normalized[0].name == "Example Corp"
    assert set(snapshot.group_edges()) == {
        (PLATFORM_ENTERPRISE_ROOT_EXTERNAL_ID, "north"),
        (PLATFORM_ENTERPRISE_ROOT_EXTERNAL_ID, "south"),
    }


@pytest.mark.parametrize(
    "departments, users, error",
    [
        ([], [], "no Groups"),
        (
            [ExternalDepartment(external_id="child", name="Child", parent_external_id="missing")],
            [],
            "missing Group",
        ),
        (
            [ExternalDepartment(external_id="root", name="Root")],
            [ExternalUser(external_id="person", name="Person", department_ids=["missing"])],
            "missing Group",
        ),
        (
            [
                ExternalDepartment(external_id="a", name="A", parent_external_id="b"),
                ExternalDepartment(external_id="b", name="B", parent_external_id="a"),
            ],
            [],
            "cycle",
        ),
    ],
)
def test_vendor_snapshot_rejects_incomplete_or_cyclic_graph(departments, users, error):
    with pytest.raises(ScimSnapshotError, match=error):
        build_vendor_scim_snapshot(departments, users)
