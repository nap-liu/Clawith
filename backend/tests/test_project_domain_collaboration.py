from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.agent_context import build_agent_context
from app.services.agent_runtime_workspace import (
    bind_agent_runtime_workspace,
    project_agent_runtime_workspace,
)
from app.services.llm.compactor import SUMMARY_SYSTEM_PROMPT
from app.services.project_collaboration_prompt import (
    PROJECT_COLLABORATION_CONTRACT,
    PROJECT_HUMAN_REQUEST_MAX_CHARS,
    PROJECT_WORK_ITEM_CRITERIA_MAX_ITEMS,
    PROJECT_WORK_ITEM_DESCRIPTION_MAX_CHARS,
    PROJECT_WORK_ITEM_EVIDENCE_MAX_ITEMS,
    build_project_a2a_task,
    build_project_group_task,
    build_project_owner_batch_task,
    build_project_runtime_context,
    project_professional_lens,
)
from app.services.project_reply_quality import (
    assess_project_reply,
    build_project_reply_correction_prompt,
    project_handoff_rejection_reasons,
)


class _ProjectAssetStorage:
    def __init__(self, files: dict[str, str]):
        self.files = files

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def list_dir(self, _key: str) -> list:
        return []

    async def read_text(self, key: str, **_kwargs) -> str:
        return self.files[key]


class _RuntimeSessionContext:
    def __init__(self, runtime: dict[str, object]):
        self._runtime = runtime

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _model, _session_id):
        return SimpleNamespace(im_config=self._runtime)


DOMAIN_CASES = (
    {
        "role": "数据分析师",
        "soul": "DATA_SOUL：先校验口径和样本偏差，再解释指标。",
        "memory": "DATA_MEMORY：基准转化率来自 workspace/data/funnel.csv。",
        "request": (
            "分析转化率下降；当前分母口径未确认。引用 workspace/data/funnel.csv，先验证口径，再决定是否调整投放。"
        ),
    },
    {
        "role": "客服负责人",
        "soul": "SUPPORT_SOUL：保护客户体验，同时识别样本与升级偏差。",
        "memory": "SUPPORT_MEMORY：SLA 证据在 workspace/support/tickets.csv。",
        "request": (
            "判断 SLA 告警是否需要升级；当前投诉样本可能偏置。引用 workspace/support/tickets.csv，并给出升级决策。"
        ),
    },
    {
        "role": "运维负责人",
        "soul": "OPS_SOUL：优先控制故障半径，变更必须可回滚。",
        "memory": "OPS_MEMORY：回滚证据关联 run 829d2746 和 event evt-ops-17。",
        "request": ("错误预算已耗尽，判断继续发布还是回滚。核对 run 829d2746 与 event evt-ops-17，明确决策和执行人。"),
    },
)

ROLE_LENS_CASES = (
    ("产品经理", "user and business outcome"),
    ("UI/UX 设计师", "end-to-end user flow"),
    ("前端研发工程师", "UI state model and API contract"),
    ("后端研发工程师", "data model and API contract"),
    ("测试工程师", "risk-based verification"),
    ("SRE 运维工程师", "service level impact"),
    ("营销增长经理", "audience, value proposition"),
    ("数据分析师", "metric and denominator"),
    ("人力资源专家", "fairness, privacy"),
    ("客户成功经理", "customer impact, severity and SLA"),
    ("营运负责人", "operating flow, capacity and exception paths"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DOMAIN_CASES, ids=("data", "support", "operations"))
async def test_project_role_soul_and_core_memory_reach_the_standard_runtime(case: dict[str, str]):
    """Project-owned role identity must survive at the final LLM context boundary."""

    agent_id = uuid.uuid4()
    workspace = project_agent_runtime_workspace(
        agent_id=agent_id,
        tenant_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
    )
    storage = _ProjectAssetStorage(
        {
            workspace.storage_key("soul.md"): f"# Soul\n{case['soul']}",
            workspace.storage_key("memory/memory.md"): case["memory"],
        }
    )

    with (
        bind_agent_runtime_workspace(workspace),
        patch("app.services.agent_context.get_storage_backend", return_value=storage),
        patch("app.services.agent_memory.get_storage_backend", return_value=storage),
        patch(
            "app.services.agent_context._load_skills_index",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.services.agent_context._collect_extension_prompts",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.agent_context._load_relationships_from_db",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.services.timezone_utils.get_agent_timezone",
            new_callable=AsyncMock,
            return_value="UTC",
        ),
    ):
        static, dynamic = await build_agent_context(
            agent_id,
            case["role"],
            role_description=case["role"],
        )

    assert case["soul"] in static
    assert case["memory"] in dynamic
    assert f"## Role\n{case['role']}" in static


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DOMAIN_CASES, ids=("data", "support", "operations"))
async def test_project_assignment_reaches_the_final_standard_context(case: dict[str, str]):
    """The immutable project assignment must be present at the final prompt boundary."""

    agent_id = uuid.uuid4()
    project_id = uuid.uuid4()
    session_id = uuid.uuid4()
    workspace = project_agent_runtime_workspace(
        agent_id=agent_id,
        tenant_id=uuid.uuid4(),
        project_id=project_id,
    )
    storage = _ProjectAssetStorage(
        {
            workspace.storage_key("soul.md"): f"# Soul\n{case['soul']}",
            workspace.storage_key("memory/memory.md"): case["memory"],
        }
    )
    runtime = {
        "project_id": str(project_id),
        "project_group_session_id": str(uuid.uuid4()),
        "project_name_snapshot": "跨领域真实验收",
        "project_goal_snapshot": case["request"],
        "project_success_criteria_snapshot": ["结论有证据", "保留专业分歧"],
        "project_member_name_snapshot": case["role"],
        "project_member_role_snapshot": case["role"],
        "project_role_snapshot": "participant",
        "capability_snapshot": [],
    }

    with (
        bind_agent_runtime_workspace(workspace),
        patch("app.database.async_session", return_value=_RuntimeSessionContext(runtime)),
        patch("app.services.agent_context.get_storage_backend", return_value=storage),
        patch("app.services.agent_memory.get_storage_backend", return_value=storage),
        patch(
            "app.services.agent_context._load_skills_index",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.services.agent_context._collect_extension_prompts",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.agent_context._load_relationships_from_db",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.services.timezone_utils.get_agent_timezone",
            new_callable=AsyncMock,
            return_value="UTC",
        ),
    ):
        static, dynamic = await build_agent_context(
            agent_id,
            case["role"],
            role_description=case["role"],
            channel_context={"session_id": str(session_id)},
        )

    assert case["soul"] in static
    assert case["memory"] in dynamic
    assert "## Project Runtime Boundary" in dynamic
    assert f"project_id: {project_id}" in dynamic
    assert f"Your immutable project role: {case['role']}" in dynamic
    assert case["request"] in dynamic
    assert "not a generic acknowledgement or activity log" in dynamic
    assert "overrides any older project, client, or role context" in dynamic


@pytest.mark.asyncio
async def test_project_identity_boundary_remains_when_core_memory_is_excluded():
    agent_id = uuid.uuid4()
    workspace = project_agent_runtime_workspace(
        agent_id=agent_id,
        tenant_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
    )
    storage = _ProjectAssetStorage({workspace.storage_key("soul.md"): "# Soul\n专业判断优先于进度汇报。"})

    with (
        bind_agent_runtime_workspace(workspace),
        patch("app.services.agent_context.get_storage_backend", return_value=storage),
        patch("app.services.agent_memory.get_storage_backend", return_value=storage),
        patch(
            "app.services.agent_context._load_skills_index",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.services.agent_context._collect_extension_prompts",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.agent_context._load_relationships_from_db",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.services.timezone_utils.get_agent_timezone",
            new_callable=AsyncMock,
            return_value="UTC",
        ),
    ):
        static, dynamic = await build_agent_context(
            agent_id,
            "风险分析师",
            role_description="负责风险识别与证据审查",
            include_memory=False,
        )

    assert "project owner controls both this core memory and `soul.md`" in static
    assert "## Project Memory" not in dynamic


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=("data", "support", "operations"))
def test_cross_domain_group_and_a2a_tasks_preserve_evidence_and_role_judgment(case: dict[str, str]):
    group_task = build_project_group_task(case["request"], is_owner=False)
    a2a_task = build_project_a2a_task(
        case["request"],
        source_name="产品负责人",
        source_role="负责目标取舍和范围决策",
        target_name=case["role"],
        target_role=case["role"],
        work_item_title="验证关键业务判断",
    )

    for task in (group_task, a2a_task):
        assert task.startswith(case["request"])
        assert PROJECT_COLLABORATION_CONTRACT in task
        assert "challenge material assumptions" in task
        assert "verified evidence" in task
        assert "concrete recommendation or decision" in task
    assert f"Recipient: {case['role']} — {case['role']}" in a2a_task
    assert f"Recipient professional decision lens: {project_professional_lens(case['role'])}" in a2a_task
    assert "do not merely acknowledge, restate, or report progress" in a2a_task
    assert "wake another role merely to announce progress" in a2a_task
    assert "professional question or an expected result" in a2a_task


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=("data", "support", "operations"))
def test_project_runtime_context_pins_identity_goal_and_professional_duty(case: dict[str, str]):
    context = build_project_runtime_context(
        {
            "project_name_snapshot": "跨领域真实验收",
            "project_goal_snapshot": case["request"],
            "project_success_criteria_snapshot": ["结论有证据", "保留专业分歧"],
            "project_member_name_snapshot": case["role"],
            "project_member_role_snapshot": case["role"],
            "project_role_snapshot": "participant",
        }
    )

    assert "跨领域真实验收" in context
    assert case["request"] in context
    assert f"Your immutable project role: {case['role']}" in context
    assert f"Your mandatory professional decision lens: {project_professional_lens(case['role'])}" in context
    assert "结论有证据" in context
    assert "not a generic acknowledgement or activity log" in context


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=("data", "support", "operations"))
def test_owner_batch_preserves_source_attribution_dissent_and_decision(case: dict[str, str]):
    source_agent_id = str(uuid.uuid4())
    task = build_project_owner_batch_task(
        batch_id="batch-domain-1",
        group_session_id=str(uuid.uuid4()),
        replies=(
            {
                "source_agent_id": source_agent_id,
                "content": case["request"],
                "project_run_ids": ["run-domain-1"],
            },
        ),
    )

    assert source_agent_id in task
    assert case["request"] in task
    assert "Attribute evidence and conclusions to their source member" in task
    assert "preserve material dissent" in task
    assert "make the next project decision" in task


def test_collaboration_tasks_bound_and_preserve_causal_work_item_context():
    long_value = "证据" * 2_000
    snapshot = {
        "id": str(uuid.uuid4()),
        "title": "审查上线风险",
        "description": long_value,
        "status": "in_progress",
        "acceptance_criteria": [f"条件 {index} {long_value}" for index in range(20)],
        "dependencies": [{"id": str(uuid.uuid4()), "title": f"依赖 {index}", "status": "done"} for index in range(20)],
        "evidence": [f"证据 {index} {long_value}" for index in range(20)],
    }
    a2a_task = build_project_a2a_task(
        "判断是否可以上线",
        source_name="产品负责人",
        source_role="范围与风险决策",
        target_name="质量负责人",
        target_role="独立质量验收",
        work_item_snapshot=snapshot,
    )
    owner_task = build_project_owner_batch_task(
        batch_id="batch-bounded-context",
        group_session_id=str(uuid.uuid4()),
        replies=({"content": "不建议上线", "source_agent_id": str(uuid.uuid4())},),
        original_human_request={"message_id": str(uuid.uuid4()), "content": long_value},
        work_item_snapshots=(snapshot,),
    )

    for task in (a2a_task, owner_task):
        assert "审查上线风险" in task
        assert "条件 0" in task
        assert "依赖 0" in task
        assert "证据 0" in task
        assert long_value not in task
    assert len("证据" * 1_000) == PROJECT_HUMAN_REQUEST_MAX_CHARS
    assert len(snapshot["description"]) > PROJECT_WORK_ITEM_DESCRIPTION_MAX_CHARS
    assert f"条件 {PROJECT_WORK_ITEM_CRITERIA_MAX_ITEMS}" not in a2a_task
    assert f"证据 {PROJECT_WORK_ITEM_EVIDENCE_MAX_ITEMS}" not in a2a_task


def test_standard_context_compaction_preserves_project_role_semantics():
    """All project sessions use the standard compactor, so its contract is pinned."""

    assert "speaker/Agent/role" in SUMMARY_SYSTEM_PROMPT
    assert "Role-specific judgments" in SUMMARY_SYSTEM_PROMPT
    assert "Evidence attribution" in SUMMARY_SYSTEM_PROMPT
    assert "next actions and their owners" in SUMMARY_SYSTEM_PROMPT


@pytest.mark.parametrize(("role", "expected"), ROLE_LENS_CASES)
def test_project_roles_receive_distinct_professional_decision_lenses(role: str, expected: str):
    lens = project_professional_lens(role)

    assert expected in lens
    assert "generic status" not in lens


@pytest.mark.parametrize(
    "reply",
    (
        "收到，我会继续推进。",
        "已完成，相关内容已更新。",
        "Acknowledged. Working on it.",
        "Status update: task completed.",
        "The task has been completed, records were updated, and the team is waiting for next steps.",
        "已完成需求分析，已更新工作项，等待下一步安排。",
        "当前进度 50%，下一步继续推进。",
        "Now writing all 4 deliverables. 结论：列表页不应使用三重风险编码。",
        "Let me update the work item. 依据 `docs/prd.md`，状态应为 7 态。",
        "现在我将写入交付文档。结论：应采用锨点长页。",
    ),
)
def test_project_reply_quality_guard_catches_only_mechanical_updates(reply: str):
    assert assess_project_reply(reply).needs_correction is True


@pytest.mark.parametrize(
    "reply",
    (
        "结论：不应上线。依据 run 829d2746 的错误率为 12%，超过 2% 阈值；建议运维负责人立即回滚。",
        "已完成口径校验：分母应采用支付成功用户，因为曝光用户会放大渠道差异；建议按该口径重算。",
        "收到。结论：索引方案不可上线，因为写放大风险未验证。",
        "已完成：commit a1b2c3d4 包含迁移与回滚脚本，测试结果记录在 `tests/report.md`。",
        (
            "Received samples are biased because enterprise tickets are over-represented; "
            "recommend stratified sampling before escalation."
        ),
        "缺少生产数据库的只读凭据，无法判断索引命中率；它会改变扩容决策。负责人能否提供脱敏 EXPLAIN？",
    ),
)
def test_project_reply_quality_guard_preserves_professional_judgment(reply: str):
    assert assess_project_reply(reply).needs_correction is False


def test_project_reply_correction_is_explicitly_bounded_and_non_broadcasting():
    prompt = build_project_reply_correction_prompt(
        "收到，我会继续推进。",
        role="数据分析师",
    )

    assert "single bounded self-correction" in prompt
    assert "do not send messages, wake Agents, or repeat tool actions" in prompt
    assert "Your immutable project role is: 数据分析师" in prompt
    assert "metric and denominator" in prompt
    assert "Previous low-value answer" in prompt


def test_project_handoff_gate_blocks_passive_wakes_but_preserves_professional_requests():
    assert project_handoff_rejection_reasons("已完成，已更新工作项，请收到后等待。") == (
        "status_only",
        "activity_ledger",
    )
    assert (
        project_handoff_rejection_reasons(
            "请按 `docs/release.md` 的 2% 错误率阈值独立审查 run a1b2c3d4，返回上线或回滚决策并说明残余风险。"
        )
        == ()
    )
