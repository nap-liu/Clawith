import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "acceptance" / "run_cross_domain_projects.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("cross_domain_acceptance_runner", RUNNER_PATH)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
RUNNER_MODULE = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = RUNNER_MODULE
RUNNER_SPEC.loader.exec_module(RUNNER_MODULE)
AcceptanceRunner = RUNNER_MODULE.AcceptanceRunner
Scenario = RUNNER_MODULE.Scenario


def test_acceptance_matrix_covers_eight_distinct_business_domains() -> None:
    scenarios = {scenario.key: scenario for scenario in RUNNER_MODULE.SCENARIOS}
    required = {
        "development",
        "product_ux",
        "marketing",
        "data",
        "hr",
        "operations",
        "support",
        "release_operations",
    }

    assert required <= scenarios.keys()
    selected = [scenarios[key] for key in sorted(required)]
    assert len({scenario.input_content for scenario in selected}) == len(selected)
    assert len({scenario.collaboration for scenario in selected}) == len(selected)
    assert len({scenario.roles for scenario in selected}) == len(selected)
    assert len({scenario.outputs for scenario in selected}) == len(selected)


def test_matrix_configuration_is_self_validating_and_auditable() -> None:
    RUNNER_MODULE.validate_matrix()
    manifest = RUNNER_MODULE.describe_matrix()

    assert manifest["scenario_count"] == len(RUNNER_MODULE.SCENARIOS) >= 8
    for row in manifest["scenarios"]:
        assert row["input"]["content"].strip()
        assert len(row["roles"]) >= 5
        assert all(role["responsibility"].strip() for role in row["roles"])
        assert row["deliverables"]
        assert all(deliverable["owner_role"] for deliverable in row["deliverables"])
        assert all(deliverable["purpose"] for deliverable in row["deliverables"])


def test_domain_artifacts_use_real_business_formats_instead_of_summary_only_outputs() -> None:
    scenarios = {scenario.key: scenario for scenario in RUNNER_MODULE.SCENARIOS}
    expected_suffixes = {
        "development": {".py", ".tsx", ".yaml"},
        "product_ux": {".html", ".md"},
        "marketing": {".csv", ".md"},
        "data": {".sql", ".csv", ".md"},
        "hr": {".csv", ".md"},
        "operations": {".csv", ".md"},
        "support": {".csv", ".md"},
        "release_operations": {".yaml", ".md"},
    }

    for key, required_suffixes in expected_suffixes.items():
        actual_suffixes = {Path(path).suffix for path in scenarios[key].outputs}
        assert required_suffixes <= actual_suffixes, key


def test_matrix_validation_rejects_an_owner_outside_the_project_team() -> None:
    source = RUNNER_MODULE.SCENARIOS[0]
    invalid_contract = RUNNER_MODULE.DeliverableContract(
        path=source.outputs[0],
        owner_role="marketing_owner",
        purpose="invalid",
        concepts=("one", "two", "three"),
    )
    invalid = RUNNER_MODULE.Scenario(
        key="invalid",
        name="invalid",
        description="invalid",
        goal="invalid",
        criteria=("invalid",),
        roles=source.roles,
        input_path="inputs/invalid.md",
        input_content="input",
        collaboration="invalid",
        outputs=(invalid_contract.path,),
        deliverables=(invalid_contract,),
    )

    with pytest.raises(ValueError, match="owner is not a project role"):
        RUNNER_MODULE.validate_matrix((invalid,))


@pytest.mark.parametrize("scenario", RUNNER_MODULE.SCENARIOS, ids=lambda item: item.key)
def test_every_domain_has_real_role_and_deliverable_contracts(scenario: Scenario) -> None:
    contracts = RUNNER_MODULE.scenario_deliverables(scenario)

    assert len(scenario.roles) >= 5
    assert len(set(scenario.roles)) == len(scenario.roles)
    assert all(role in RUNNER_MODULE.ROLES for role in scenario.roles)
    assert {contract.path for contract in contracts} == set(scenario.outputs)
    assert all(contract.owner_role in scenario.roles for contract in contracts)
    assert all(contract.review_role in scenario.roles for contract in contracts if contract.review_role)
    assert all(contract.purpose.strip() for contract in contracts)
    assert all(len(contract.concepts) >= 3 for contract in contracts)


@pytest.mark.parametrize("scenario", RUNNER_MODULE.SCENARIOS, ids=lambda item: item.key)
def test_planning_prompt_transmits_professional_roles_and_handoffs(scenario: Scenario) -> None:
    runner = AcceptanceRunner.__new__(AcceptanceRunner)
    prompt = runner.planning_prompt(scenario)

    for role_key in scenario.roles:
        role = RUNNER_MODULE.ROLES[role_key]
        assert role.name in prompt
        assert role.description in prompt
    for contract in RUNNER_MODULE.scenario_deliverables(scenario):
        assert contract.path in prompt
        assert contract.purpose in prompt
        assert RUNNER_MODULE.ROLES[contract.owner_role].name in prompt
        if contract.review_role:
            assert RUNNER_MODULE.ROLES[contract.review_role].name in prompt
    assert "不得互相冒充" in prompt
    assert "专业判断" in prompt
    assert "独立复核岗位不能照抄" in prompt
    assert "点对点 A2A" in prompt
    assert "禁止广播唤醒" in prompt


def _professional_contents(scenario: Scenario) -> dict[str, str]:
    contents: dict[str, str] = {}
    critique_roles = {
        "risk",
        "stat_reviewer",
        "hr_compliance",
        "support_qa",
        "security",
        "legal",
        "independent",
        "code_reviewer",
        "qa_engineer",
        "accessibility_reviewer",
        "security_reviewer",
    }
    for index, contract in enumerate(RUNNER_MODULE.scenario_deliverables(scenario), start=1):
        critique = "风险与问题质疑后给出改进。" if contract.owner_role in critique_roles else ""
        contents[contract.path] = (
            f"# {contract.purpose}\n"
            f"岗位：{RUNNER_MODULE.ROLES[contract.owner_role].name}\n"
            f"专业内容：{'、'.join(contract.concepts)}。\n"
            f"基于输入证据和数据来源形成第 {index} 项独立结论。{critique}\n"
            "决策：采用本方案。下一步：负责人在截止日期前完成验证并保留证据。"
        )
    return contents


@pytest.mark.parametrize("scenario", RUNNER_MODULE.SCENARIOS, ids=lambda item: item.key)
def test_semantic_assessment_accepts_distinct_professional_outputs(scenario: Scenario) -> None:
    result = AcceptanceRunner._assess_deliverable_semantics(scenario, _professional_contents(scenario))

    assert result == {
        "configuration_errors": [],
        "empty_or_trivial_outputs": [],
        "concept_failures": {},
        "duplicate_output_groups": [],
        "missing_decision_signal_groups": [],
        "output_signal_failures": {},
        "review_outputs_without_challenge": [],
    }


def test_semantic_assessment_rejects_generic_same_text_for_every_role() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "product_ux")
    generic = "项目已经完成。这里是统一的工作总结，没有引用输入证据，也没有具体专业判断和下一步。"
    contents = {path: generic for path in scenario.outputs}

    result = AcceptanceRunner._assess_deliverable_semantics(scenario, contents)

    assert result["duplicate_output_groups"] == [list(scenario.outputs)]
    assert set(result["concept_failures"]) == set(scenario.outputs)
    assert result["missing_decision_signal_groups"]
    assert set(result["output_signal_failures"]) == {
        path for path in scenario.outputs if Path(path).suffix.casefold() in {".md", ".mdx", ".txt"}
    }
    assert "review/accessibility-audit.md" in result["review_outputs_without_challenge"]


@pytest.mark.parametrize("scenario", RUNNER_MODULE.SCENARIOS, ids=lambda item: item.key)
def test_role_run_assessment_accepts_distinct_professional_judgments(scenario: Scenario) -> None:
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    runs = []
    for index, role in enumerate(scenario.roles, start=1):
        concepts = {
            concept
            for contract in RUNNER_MODULE.scenario_deliverables(scenario)
            if contract.owner_role == role or contract.review_role == role
            for concept in contract.concepts
        }
        critique = "识别风险与问题并提出改进。" if role in AcceptanceRunner.INDEPENDENT_REVIEW_ROLES else ""
        result = (
            f"{RUNNER_MODULE.ROLES[role].name}的第 {index} 项专业判断。"
            f"输入证据来源已核对；{'、'.join(sorted(concepts))}。{critique}"
            "结论与决策：采用可验证方案。下一步：负责人按截止日期完成行动并保留证据。"
        )
        runs.append(
            _run(
                f"run-{role}",
                status="succeeded",
                created_at=f"2026-08-21T10:{index:02d}:00Z",
                agent_id=role_agent_ids[role],
                work_item_id=f"work-{role}",
            )
        )
        runs[-1]["output"] = {"result": result}

    assessment = AcceptanceRunner._assess_role_run_semantics(scenario, role_agent_ids, runs)

    assert assessment == {
        "missing_or_trivial_role_results": [],
        "role_signal_failures": {},
        "role_concept_failures": {},
        "duplicate_role_result_groups": [],
        "reviewers_without_challenge": [],
        "mechanical_status_role_results": [],
    }


def test_role_run_assessment_rejects_role_agnostic_progress_logs() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "development")
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    generic = "已处理任务并保存结果。这是一条没有业务证据、决策或明确后续行动的通用进度记录。"
    runs = []
    for index, role in enumerate(scenario.roles, start=1):
        runs.append(
            _run(
                f"run-{role}",
                status="succeeded",
                created_at=f"2026-08-21T11:{index:02d}:00Z",
                agent_id=role_agent_ids[role],
                work_item_id=f"work-{role}",
            )
        )
        runs[-1]["output"] = {"result": generic}

    assessment = AcceptanceRunner._assess_role_run_semantics(scenario, role_agent_ids, runs)

    assert assessment["duplicate_role_result_groups"] == [[RUNNER_MODULE.ROLES[role].name for role in scenario.roles]]
    assert assessment["role_concept_failures"]
    assert "代码审查工程师" in assessment["reviewers_without_challenge"]
    assert "测试工程师" in assessment["reviewers_without_challenge"]


def test_role_run_assessment_rejects_distinct_but_mechanical_status_acknowledgements() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "compliance")
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    runs = []
    for index, role in enumerate(scenario.roles, start=1):
        concepts = {
            concept
            for contract in RUNNER_MODULE.scenario_deliverables(scenario)
            if contract.owner_role == role or contract.review_role == role
            for concept in contract.concepts
        }
        runs.append(
            _run(
                f"run-{role}",
                status="succeeded",
                created_at=f"2026-08-21T12:{index:02d}:00Z",
                agent_id=role_agent_ids[role],
                work_item_id=f"work-{role}",
            )
        )
        runs[-1]["output"] = {
            "result": (
                f"收到，已确认项目状态同步。这是{RUNNER_MODULE.ROLES[role].name}的操作汇总："
                f"{'、'.join(sorted(concepts))}正在等待上游完成。"
            )
        }

    assessment = AcceptanceRunner._assess_role_run_semantics(scenario, role_agent_ids, runs)

    assert set(assessment["mechanical_status_role_results"]) == {
        RUNNER_MODULE.ROLES[role].name for role in scenario.roles
    }


def _conversation(
    session_id: str,
    agent_id: str,
    messages: list[dict[str, str]],
    *,
    kind: str = "run",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "session_id": session_id,
        "agent_id": agent_id,
        "messages": messages,
    }


@pytest.mark.parametrize(
    "scenario_key",
    ("marketing", "data", "hr", "operations", "support"),
)
def test_conversation_assessment_accepts_real_cross_domain_role_judgment(scenario_key: str) -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == scenario_key)
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    conversations = []
    for index, role in enumerate(scenario.roles, start=1):
        concepts = {
            concept
            for contract in RUNNER_MODULE.scenario_deliverables(scenario)
            if contract.owner_role == role or contract.review_role == role
            for concept in contract.concepts
        }
        challenge = (
            "我反对直接通过：当前证据存在限制，需比较替代方案并关闭风险。"
            if (role in AcceptanceRunner.INDEPENDENT_REVIEW_ROLES)
            else "需要权衡交付速度和验证充分性，并明确关键假设。"
        )
        content = (
            f"{RUNNER_MODULE.ROLES[role].name}判断：{'、'.join(sorted(concepts))}必须按岗位标准核验。"
            f"证据来源是项目输入文件与可复核数据；{challenge}"
            "结论与决策：采用证据更充分的方案。下一步：负责人在截止日期前完成验证并记录结果。"
        )
        conversations.append(
            _conversation(
                f"session-{role}",
                role_agent_ids[role],
                [{"id": f"message-{index}", "role": "assistant", "content": content}],
            )
        )

    assessment = AcceptanceRunner._assess_conversation_semantics(
        scenario,
        role_agent_ids,
        conversations,
    )

    assert assessment == {
        "missing_professional_dialogue_roles": [],
        "trivial_professional_turn_ids": [],
        "mechanical_professional_turn_ids": [],
        "unprofessional_a2a_request_turn_ids": [],
        "role_dialogue_signal_failures": {},
        "role_dialogue_concept_failures": {},
        "reviewers_without_dialogue_challenge": [],
        "dialogue_without_tradeoff": False,
    }


def test_conversation_assessment_rejects_status_ledger_in_every_a2a_session() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "support")
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    conversations = [
        _conversation(
            f"session-{role}",
            role_agent_ids[role],
            [
                {
                    "id": f"message-{role}",
                    "role": "assistant",
                    "content": f"收到，{RUNNER_MODULE.ROLES[role].name}状态同步，等待上游完成。",
                }
            ],
        )
        for role in scenario.roles
    ]

    assessment = AcceptanceRunner._assess_conversation_semantics(
        scenario,
        role_agent_ids,
        conversations,
    )

    assert set(assessment["trivial_professional_turn_ids"]) == {f"message-{role}" for role in scenario.roles}
    assert assessment["role_dialogue_signal_failures"]
    assert assessment["role_dialogue_concept_failures"]
    assert set(assessment["reviewers_without_dialogue_challenge"]) == {
        "客服质量与 VOC 分析",
    }
    assert assessment["dialogue_without_tradeoff"] is True


def test_conversation_assessment_rejects_long_acknowledgement_that_keyword_stuffs_status() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "data")
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    conversations = []
    for role in scenario.roles:
        concepts = {
            concept
            for contract in RUNNER_MODULE.scenario_deliverables(scenario)
            if contract.owner_role == role or contract.review_role == role
            for concept in contract.concepts
        }
        conversations.append(
            _conversation(
                f"session-{role}",
                role_agent_ids[role],
                [
                    {
                        "id": f"message-{role}",
                        "role": "assistant",
                        "content": (
                            f"收到，状态同步，操作汇总：{'、'.join(sorted(concepts))}。"
                            "当前只是等待依赖，暂不提供证据、专业结论、决策或下一步责任人。"
                        ),
                    }
                ],
            )
        )

    assessment = AcceptanceRunner._assess_conversation_semantics(
        scenario,
        role_agent_ids,
        conversations,
    )

    assert set(assessment["mechanical_professional_turn_ids"]) == {f"message-{role}" for role in scenario.roles}


def test_conversation_assessment_rejects_mechanical_a2a_request_without_professional_question() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "operations")
    role = scenario.roles[0]
    role_agent_ids = {candidate: f"agent-{candidate}" for candidate in scenario.roles}
    concepts = {
        concept
        for contract in RUNNER_MODULE.scenario_deliverables(scenario)
        if contract.owner_role == role or contract.review_role == role
        for concept in contract.concepts
    }
    conversations = [
        _conversation(
            "session-a2a",
            role_agent_ids[role],
            [
                {
                    "id": "request-mechanical",
                    "role": "user",
                    "content": "收到，状态同步：当前等待上游完成。",
                },
                {
                    "id": "response-professional",
                    "role": "assistant",
                    "content": (
                        f"{RUNNER_MODULE.ROLES[role].name}判断：{'、'.join(sorted(concepts))}必须核验。"
                        "证据来源是实时库存和履约记录；需要权衡缺货风险与履约成本。"
                        "结论与决策：采用风险更低的方案。下一步：负责人按时完成验证并记录结果。"
                    ),
                },
            ],
            kind="a2a",
        )
    ]

    assessment = AcceptanceRunner._assess_conversation_semantics(
        scenario,
        role_agent_ids,
        conversations,
    )

    assert assessment["unprofessional_a2a_request_turn_ids"] == ["request-mechanical"]


def test_conversation_assessment_accepts_concise_professional_a2a_request() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "operations")
    role = scenario.roles[0]
    role_agent_ids = {candidate: f"agent-{candidate}" for candidate in scenario.roles}
    concepts = {
        concept
        for contract in RUNNER_MODULE.scenario_deliverables(scenario)
        if contract.owner_role == role or contract.review_role == role
        for concept in contract.concepts
    }
    conversations = [
        _conversation(
            "session-a2a",
            role_agent_ids[role],
            [
                {"id": "request-professional", "role": "user", "content": "请核验库存风险并给出决策。"},
                {
                    "id": "response-professional",
                    "role": "assistant",
                    "content": (
                        f"{RUNNER_MODULE.ROLES[role].name}判断：{'、'.join(sorted(concepts))}必须核验。"
                        "证据来源是实时库存和履约记录；需要权衡缺货风险与履约成本。"
                        "结论与决策：采用风险更低的方案。下一步：负责人按时完成验证并记录结果。"
                    ),
                },
            ],
            kind="a2a",
        )
    ]

    assessment = AcceptanceRunner._assess_conversation_semantics(
        scenario,
        role_agent_ids,
        conversations,
    )

    assert assessment["unprofessional_a2a_request_turn_ids"] == []


def test_collaboration_topology_accepts_connected_role_network_and_review_handoffs() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "hr")
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    edges = (
        ("hiring_owner", "job_analyst"),
        ("job_analyst", "hr_compliance"),
        ("hiring_owner", "sourcer"),
        ("hiring_owner", "interviewer"),
        ("hiring_owner", "hr_compliance"),
        ("sourcer", "hr_compliance"),
        ("interviewer", "hr_compliance"),
    )
    conversations = [
        _conversation(
            f"session-{source}-{target}",
            role_agent_ids[target],
            [
                {
                    "id": f"request-{index}",
                    "role": "user",
                    "sender_agent_id": role_agent_ids[source],
                    "content": "请评审岗位证据、说明专业异议并给出建议。",
                }
            ],
            kind="a2a",
        )
        for index, (source, target) in enumerate(edges)
    ]

    assessment = AcceptanceRunner._assess_collaboration_topology(
        scenario,
        role_agent_ids,
        conversations,
    )

    assert assessment["professional_a2a_edge_count"] == len(edges)
    assert assessment["professional_requests_without_sender_ids"] == []
    assert assessment["isolated_collaboration_roles"] == []
    assert assessment["disconnected_collaboration_roles"] == []
    assert assessment["missing_review_handoffs"] == []


def test_collaboration_topology_rejects_parallel_silos_and_missing_review_exchange() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "data")
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    conversations = [
        _conversation(
            "session-owner-engineer",
            role_agent_ids["data_engineer"],
            [
                {
                    "id": "request-owner-engineer",
                    "role": "user",
                    "sender_agent_id": role_agent_ids["analytics_owner"],
                    "content": "请分析数据质量证据并提出下一步建议。",
                }
            ],
            kind="a2a",
        )
    ]

    assessment = AcceptanceRunner._assess_collaboration_topology(
        scenario,
        role_agent_ids,
        conversations,
    )

    assert set(assessment["isolated_collaboration_roles"]) == {
        "数据分析师",
        "统计评审",
        "BI 设计师",
    }
    assert set(assessment["disconnected_collaboration_roles"]) == {
        "数据分析师",
        "统计评审",
        "BI 设计师",
    }
    assert assessment["missing_review_handoffs"]


def test_collaboration_topology_rejects_unattributed_professional_a2a_request() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "support")
    role_agent_ids = {role: f"agent-{role}" for role in scenario.roles}
    conversations = [
        _conversation(
            "session-unattributed",
            role_agent_ids["triage"],
            [
                {
                    "id": "request-unattributed",
                    "role": "user",
                    "content": "请核验 P0 工单的 SLA 证据并给出升级建议。",
                }
            ],
            kind="a2a",
        )
    ]

    assessment = AcceptanceRunner._assess_collaboration_topology(
        scenario,
        role_agent_ids,
        conversations,
    )

    assert assessment["professional_requests_without_sender_ids"] == ["request-unattributed"]


def test_load_project_conversations_uses_group_and_exact_run_session_public_apis() -> None:
    scenario = next(item for item in RUNNER_MODULE.SCENARIOS if item.key == "marketing")
    runner = _runner_for_project(scenario)
    requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        requests.append((method, path, kwargs))
        if "/group-sessions/" in path:
            return {"items": [{"id": "group-message", "role": "assistant", "content": "group"}]}
        return [{"id": "run-message", "role": "assistant", "content": "run"}]

    runner.request = request
    conversations = asyncio.run(
        runner._load_project_conversations(
            project_id="project-1",
            group_session_id="group-1",
            runs=[
                _run(
                    "run-1",
                    status="succeeded",
                    created_at="2026-08-21T10:00:00Z",
                    agent_id="agent-1",
                )
                | {"session_id": "session-1"},
                _run(
                    "run-2",
                    status="succeeded",
                    created_at="2026-08-21T10:01:00Z",
                    agent_id="agent-1",
                )
                | {"session_id": "session-1"},
            ],
        )
    )

    assert [conversation["kind"] for conversation in conversations] == ["group", "a2a"]
    assert requests == [
        (
            "GET",
            "/projects/project-1/group-sessions/group-1/messages",
            {"params": {"limit": 500}},
        ),
        (
            "GET",
            "/agents/agent-1/sessions/session-1/messages",
            {"params": {"limit": 500}},
        ),
    ]


def _run(
    run_id: str,
    *,
    status: str,
    created_at: str,
    trigger_type: str = "a2a",
    agent_id: str = "agent-1",
    work_item_id: str | None = None,
    run_input: dict | None = None,
) -> dict:
    return {
        "id": run_id,
        "status": status,
        "created_at": created_at,
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "work_item_id": work_item_id,
        "input": run_input or {},
        "output": {},
    }


def test_unrecovered_failed_runs_accepts_later_success_for_same_a2a_session() -> None:
    failed = _run(
        "failed",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        run_input={"session_id": "session-1"},
    )
    recovered = _run(
        "recovered",
        status="succeeded",
        created_at="2026-08-21T10:01:00Z",
        run_input={"session_id": "session-1"},
    )

    assert AcceptanceRunner._unrecovered_failed_runs([failed, recovered]) == []


def test_unrecovered_failed_runs_rejects_unrelated_later_success() -> None:
    failed = _run(
        "failed",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        run_input={"session_id": "session-1"},
    )
    unrelated = _run(
        "unrelated",
        status="succeeded",
        created_at="2026-08-21T10:01:00Z",
        run_input={"session_id": "session-2"},
    )

    assert AcceptanceRunner._unrecovered_failed_runs([failed, unrelated]) == ["failed"]


def test_unrecovered_failed_runs_accepts_explicit_retry() -> None:
    failed = _run(
        "failed",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        trigger_type="leader_reply_batch",
        run_input={"group_session_id": "group-1"},
    )
    retry = _run(
        "retry",
        status="succeeded",
        created_at="2026-08-21T10:01:00Z",
        trigger_type="retry",
        run_input={"retry_of_run_id": "failed"},
    )

    assert AcceptanceRunner._unrecovered_failed_runs([failed, retry]) == []


def test_unrecovered_failed_runs_matches_work_item_before_trigger_type() -> None:
    failed = _run(
        "failed",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        trigger_type="a2a",
        work_item_id="work-1",
    )
    recovered = _run(
        "recovered",
        status="succeeded",
        created_at="2026-08-21T10:01:00Z",
        trigger_type="retry",
        agent_id="agent-2",
        work_item_id="work-1",
    )

    assert AcceptanceRunner._unrecovered_failed_runs([failed, recovered]) == []


def _runner_for_project(scenario: Scenario) -> AcceptanceRunner:
    runner = AcceptanceRunner.__new__(AcceptanceRunner)
    runner.state = {
        "prefix": "test",
        "agents": {},
        "projects": {scenario.key: {"project_id": "project-1"}},
    }
    runner.save = lambda: None
    return runner


def test_retry_failed_run_uses_public_api_and_does_not_invent_work_item() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run(
        "failed-1",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        trigger_type="a2a",
        agent_id="agent-9",
        run_input={"dispatch": {"task": "Review the exact campaign evidence"}},
    )
    failed["title"] = "Campaign evidence review"
    requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        requests.append((method, path, kwargs))
        if method == "GET" and path.endswith("/runs"):
            return [failed]
        if method == "GET" and path == "/projects/project-1":
            return {"id": "project-1", "status": "running"}
        if method == "POST" and path.endswith("/runs"):
            return {"id": "retry-1", **kwargs["json"], "status": "queued"}
        raise AssertionError((method, path, kwargs))

    runner.request = request
    result = asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))

    assert result["id"] == "retry-1"
    post_payload = next(kwargs["json"] for method, _, kwargs in requests if method == "POST")
    assert post_payload == {
        "work_item_id": None,
        "agent_id": "agent-9",
        "trigger_type": "retry",
        "input": {
            "retry_of_run_id": "failed-1",
            "title": "Campaign evidence review",
            "objective": "Review the exact campaign evidence",
        },
    }
    assert runner.state["projects"][scenario.key]["retries"] == {"failed-1": "retry-1"}


def test_retry_failed_run_is_idempotent_when_public_api_already_has_retry() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run("failed-1", status="failed", created_at="2026-08-21T10:00:00Z")
    existing = _run(
        "retry-1",
        status="running",
        created_at="2026-08-21T10:01:00Z",
        trigger_type="retry",
        run_input={"retry_of_run_id": "failed-1"},
    )
    methods: list[str] = []

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        del path, kwargs
        methods.append(method)
        return [failed, existing]

    runner.request = request
    result = asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))

    assert result == existing
    assert methods == ["GET"]


def test_retry_failed_run_preserves_exact_work_item_and_agent() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run(
        "failed-1",
        status="cancelled",
        created_at="2026-08-21T10:00:00Z",
        agent_id="agent-2",
        work_item_id="work-7",
        run_input={"objective": "Complete the assigned deliverable"},
    )
    posted: dict[str, Any] = {}

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET" and path.endswith("/runs"):
            return [failed]
        if method == "GET":
            return {"status": "running"}
        posted.update(kwargs["json"])
        return {"id": "retry-2", "status": "queued"}

    runner.request = request
    asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))

    assert posted["work_item_id"] == "work-7"
    assert posted["agent_id"] == "agent-2"
    assert posted["input"]["retry_of_run_id"] == "failed-1"


@pytest.mark.parametrize("status", ["queued", "running", "waiting", "succeeded"])
def test_retry_failed_run_rejects_non_terminal_failure_states(status: str) -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    source = _run("run-1", status=status, created_at="2026-08-21T10:00:00Z")

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        del method, path, kwargs
        return [source]

    runner.request = request
    with pytest.raises(RuntimeError, match="only failed or cancelled"):
        asyncio.run(runner.retry_failed_run((scenario,), "run-1"))


def test_retry_failed_run_rejects_completed_project() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run("failed-1", status="failed", created_at="2026-08-21T10:00:00Z")

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        del kwargs
        if method == "GET" and path.endswith("/runs"):
            return [failed]
        return {"status": "completed"}

    runner.request = request
    with pytest.raises(RuntimeError, match="public execution retries require a running project"):
        asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))


def test_retry_failed_run_rejects_run_without_responsible_agent() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run("failed-1", status="failed", created_at="2026-08-21T10:00:00Z", agent_id="")

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        del kwargs
        if method == "GET" and path.endswith("/runs"):
            return [failed]
        return {"status": "running"}

    runner.request = request
    with pytest.raises(RuntimeError, match="no responsible Agent"):
        asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))


def test_verify_only_does_not_provision_or_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    calls: list[str] = []

    class FakeRunner:
        def __init__(self) -> None:
            self.state = {
                "projects": {
                    scenario.key: {
                        "project_id": "project-1",
                        "verification": {"passed": True},
                    }
                }
            }

        def require_prepared_projects(self, scenarios: tuple[Scenario, ...]) -> None:
            assert scenarios == (scenario,)
            calls.append("require")

        async def verify(self, selected: Scenario) -> dict[str, Any]:
            assert selected == scenario
            calls.append("verify")
            return {"passed": True}

        async def ensure_agents(self) -> None:
            raise AssertionError("verify-only must not provision Agents")

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(RUNNER_MODULE, "AcceptanceRunner", FakeRunner)
    args = argparse.Namespace(
        scenario=[scenario.key],
        batch_size=3,
        timeout=60,
        launch_only=False,
        verify_only=True,
        retry_run=None,
        continue_monitor=False,
    )

    asyncio.run(RUNNER_MODULE.run(args))

    assert calls == ["require", "verify", "close"]
