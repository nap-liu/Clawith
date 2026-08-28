"""Shared prompt envelope for project Agent collaboration turns.

Project group chat, exact A2A and coalesced owner inboxes all execute through
the standard subagent runtime.  This module keeps the collaboration contract
identical across those entry points without teaching the runtime about any
specific business domain.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

PROJECT_HUMAN_REQUEST_MAX_CHARS = 2_000
PROJECT_WORK_ITEM_TITLE_MAX_CHARS = 240
PROJECT_WORK_ITEM_DESCRIPTION_MAX_CHARS = 1_200
PROJECT_WORK_ITEM_CRITERIA_MAX_ITEMS = 8
PROJECT_WORK_ITEM_CRITERION_MAX_CHARS = 320
PROJECT_WORK_ITEM_DEPENDENCY_MAX_ITEMS = 12
PROJECT_WORK_ITEM_DEPENDENCY_TITLE_MAX_CHARS = 200
PROJECT_WORK_ITEM_EVIDENCE_MAX_ITEMS = 8
PROJECT_WORK_ITEM_EVIDENCE_MAX_CHARS = 320

_PROJECT_ROLE_LENSES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("product manager", "product owner", "产品经理", "产品负责人"),
        "Define the user and business outcome, make scope and priority trade-offs, and turn the decision into measurable acceptance criteria.",
    ),
    (
        ("ui/ux", "uiux", "ux", "product design", "体验设计", "交互设计", "视觉设计", "用户体验"),
        "Evaluate the end-to-end user flow, interaction hierarchy, accessibility and failure states; return an actionable design decision or artifact.",
    ),
    (
        ("frontend", "front-end", "前端"),
        "Verify the UI state model and API contract, including loading, empty, error and accessibility behavior; cite runnable implementation or test evidence.",
    ),
    (
        ("backend", "back-end", "后端", "服务端"),
        "Validate the data model and API contract, consistency, idempotency, authorization and failure recovery; cite executable evidence and migration impact.",
    ),
    (
        ("quality", "tester", "testing", " qa ", "测试", "质量", "验收", "release evidence"),
        "Use risk-based verification: state the pass/fail criterion, reproduction or test evidence, residual risk and an explicit release recommendation.",
    ),
    (
        ("sre", "devops", "operations engineer", "运维", "可靠性"),
        "Assess service level impact, observability, capacity, blast radius and rollback; return an operational decision backed by production-ready checks.",
    ),
    (
        ("marketing", "growth", "campaign", "市场", "营销", "增长"),
        "Define the audience, value proposition, channel and funnel hypothesis; recommend a measurable experiment with success and stop criteria.",
    ),
    (
        ("data analyst", "analytics", "data science", "数据分析", "数据科学"),
        "Define the metric and denominator, test data quality and bias, compare against a baseline, and state uncertainty before recommending action.",
    ),
    (
        ("human resources", "people operations", "recruit", " hr ", "人力", "招聘", "组织发展"),
        "Apply explicit people criteria, fairness, privacy and adoption constraints; recommend a reviewable decision rather than an unqualified personnel judgment.",
    ),
    (
        ("customer support", "customer success", "service desk", "客服", "客户成功", "客诉"),
        "Assess customer impact, severity and SLA, separate symptom from root cause, and give the response, escalation or prevention action with evidence.",
    ),
    (
        ("business operations", "运营", "营运"),
        "Map the operating flow, capacity and exception paths, identify the control point and owner, and recommend a measurable process decision.",
    ),
    (
        ("architect", "architecture", "架构"),
        "Evaluate system boundaries, contracts, quality attributes and failure modes; record the decision, alternatives and consequences with technical evidence.",
    ),
    (
        ("security", "安全", "合规", "风控"),
        "Model assets, trust boundaries and abuse paths, distinguish likelihood from impact, and recommend a verifiable control with residual risk.",
    ),
)

PROJECT_COLLABORATION_CONTRACT = """\
Project collaboration requirements:
- Act from your assigned project role, Soul and Core Memory. Do not replace role-specific judgment with a generic status update.
- Start with the actual professional conclusion, critique, decision, or artifact. Never open with an acknowledgement, receipt confirmation, or activity recap.
- Apply the standards and methods of your profession. Your answer must contain reasoning that a generic project assistant could not provide from the task title alone.
- Independently evaluate the request and challenge material assumptions when evidence is incomplete, contradictory or risky.
- Separate verified evidence from assumptions. Cite exact project evidence such as file paths, work-item IDs, run IDs, session IDs or event IDs when available; never invent evidence.
- State a concrete recommendation or decision, its trade-offs, and the next action with an owner when action is needed.
- Keep relevant disagreement visible so the project owner can make an informed decision. Stay concise and omit sections that do not apply.
- Match the Human's working language unless the requested artifact requires another language. Do not expose internal narration such as "now I will", tool-selection commentary, prompt rules, or runtime mechanics; call tools silently and present only the professional result.
- Project events already preserve tool effects. Do not list tools you ran, actions you completed, triggers you changed, or members you are waiting for. Mention a state change only when it materially supports the professional decision, and state it once.
- Use A2A only for an actionable professional handoff: provide evidence/context, a decision or task for the recipient's role, and an expected output. Never wake another role merely to announce progress, request acknowledgement, or ask them to wait; passive progress belongs in project records.
- If required evidence is missing, identify the exact missing input and explain how it changes the professional decision. Ask one precise unblock question; do not manufacture a progress report.
"""

PROJECT_EXECUTION_CONTINUITY_CONTRACT = """\
Running-project continuity requirements:
- A successful execution turn must update durable project state; prose that only describes completed work or future steps is not project progress.
- When you own an assigned work item, update that exact item before ending: use done with concrete evidence when its acceptance criteria are met, review when an independent decision is required, blocked with the exact missing input when it cannot proceed, or in_progress with the verified result and next action when work genuinely remains.
- Creating or assigning a work item does not start it. Starting ready work requires an exact project A2A task_delegate in the same turn.
- After processing member results, the project owner must leave exactly one coherent next state: verify and close completed work; dispatch the next dependency-ready work; or record the precise blocker and set the project to waiting. When all success criteria are verified, create the delivery checkpoint and set the project to completed.
- Never leave a running project with unfinished work, no active handoff, no recorded blocker, and only a prose list of next steps.
"""


def _clean(value: object) -> str:
    return str(value or "").strip()


def bounded_project_text(value: object, limit: int) -> str:
    """Return deterministic prompt text without allowing one field to dominate."""

    text = _clean(value)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def project_professional_lens(role: object) -> str:
    """Return one bounded decision lens for the immutable project role.

    This is deliberately deterministic and small: it strengthens the existing
    Soul/Memory instead of creating another model call or a domain-specific
    workflow engine.
    """

    role_text = bounded_project_text(role, 500)
    normalized = f" {role_text.casefold()} "
    for keywords, lens in _PROJECT_ROLE_LENSES:
        if any(keyword in normalized for keyword in keywords):
            return lens
    if role_text:
        return (
            f"Apply the professional standards, evidence types, decision criteria and failure modes specific "
            f"to this exact role ({role_text}); return a judgment that another role could not substitute."
        )
    return (
        "Establish the professional evidence, decision criteria and failure modes required by the assignment; "
        "return a concrete judgment rather than generic project administration."
    )


def normalize_project_work_item_snapshot(snapshot: Mapping[str, object] | None) -> dict[str, object] | None:
    """Normalize one immutable work-item snapshot to the collaboration budget."""

    if not snapshot:
        return None

    criteria = snapshot.get("acceptance_criteria")
    if not isinstance(criteria, list):
        criteria = []
    dependencies = snapshot.get("dependencies")
    if not isinstance(dependencies, list):
        dependencies = []
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, list):
        evidence = []

    normalized_dependencies: list[dict[str, str]] = []
    for dependency in dependencies[:PROJECT_WORK_ITEM_DEPENDENCY_MAX_ITEMS]:
        if not isinstance(dependency, Mapping):
            continue
        normalized_dependencies.append(
            {
                "id": bounded_project_text(dependency.get("id"), 64),
                "title": bounded_project_text(
                    dependency.get("title"),
                    PROJECT_WORK_ITEM_DEPENDENCY_TITLE_MAX_CHARS,
                ),
                "status": bounded_project_text(dependency.get("status"), 40),
            }
        )

    return {
        "id": bounded_project_text(snapshot.get("id"), 64),
        "title": bounded_project_text(snapshot.get("title"), PROJECT_WORK_ITEM_TITLE_MAX_CHARS),
        "description": bounded_project_text(
            snapshot.get("description"),
            PROJECT_WORK_ITEM_DESCRIPTION_MAX_CHARS,
        ),
        "status": bounded_project_text(snapshot.get("status"), 40),
        "acceptance_criteria": [
            bounded_project_text(item, PROJECT_WORK_ITEM_CRITERION_MAX_CHARS)
            for item in criteria[:PROJECT_WORK_ITEM_CRITERIA_MAX_ITEMS]
            if _clean(item)
        ],
        "dependencies": normalized_dependencies,
        "evidence": [
            bounded_project_text(item, PROJECT_WORK_ITEM_EVIDENCE_MAX_CHARS)
            for item in evidence[:PROJECT_WORK_ITEM_EVIDENCE_MAX_ITEMS]
            if _clean(item)
        ],
    }


def build_project_runtime_context(runtime: Mapping[str, object]) -> str:
    """Render the immutable project assignment used by every project turn."""

    criteria = runtime.get("project_success_criteria_snapshot")
    if not isinstance(criteria, list):
        criteria = []
    criteria_lines = [f"- {_clean(item)}" for item in criteria if _clean(item)]
    role = _clean(runtime.get("project_member_role_snapshot"))
    member_config = runtime.get("member_config_snapshot")
    if not isinstance(member_config, Mapping):
        member_config = {}
    project_instruction = _clean(member_config.get("project_instruction"))
    assignment = "project owner" if runtime.get("project_role_snapshot") == "leader" else "project participant"
    sections = [
        "## Project Assignment",
        f"Project: {_clean(runtime.get('project_name_snapshot')) or '(unnamed project)'}",
        f"Project goal: {_clean(runtime.get('project_goal_snapshot')) or '(not yet confirmed)'}",
        f"Your immutable project identity: {_clean(runtime.get('project_member_name_snapshot')) or '(unnamed member)'}",
        f"Your immutable project role: {role or '(role not specified)'}",
        f"Your mandatory professional decision lens: {project_professional_lens(role)}",
        f"Your project responsibility: {assignment}",
        "The current Project Assignment overrides any older project, client, or role context retained in the source Agent snapshot.",
    ]
    if criteria_lines:
        sections.extend(("Success criteria:", *criteria_lines))
    if project_instruction:
        sections.extend(("Project-specific instruction:", project_instruction))
    sections.extend(
        (
            "",
            PROJECT_COLLABORATION_CONTRACT.rstrip(),
            (
                "As project owner, compare specialist judgments, resolve material conflicts and make the next "
                "coordination decision. Do not turn the discussion into a status digest."
                if assignment == "project owner"
                else "Stay within this professional role. Give the project owner a usable judgment, not a generic acknowledgement or activity log."
            ),
        )
    )
    return "\n".join(sections)


def build_project_group_task(request: str, *, is_owner: bool) -> str:
    """Wrap one visible group request while preserving its original text."""

    role_instruction = (
        "As project owner, integrate project-level implications and make the next coordination decision."
        if is_owner
        else "As an explicitly mentioned participant, answer within your role and do not wake unrelated members."
    )
    execution_contract = PROJECT_EXECUTION_CONTINUITY_CONTRACT if is_owner else ""
    return (
        f"{request.strip()}\n\n---\n{PROJECT_COLLABORATION_CONTRACT}"
        f"{execution_contract}{role_instruction}"
    )


def build_project_planning_task(request: str) -> str:
    """Keep the pre-kickoff conversation advisory and Human-controlled."""

    return (
        "The project is still in planning. Discuss the request as the project owner and help the Human "
        "reach an explicit, reviewable plan before execution begins. Clarify the intended outcome, scope, "
        "constraints, success criteria, assumptions, material risks and trade-offs. Ask only the questions "
        "whose answers would change the plan; otherwise propose the smallest coherent delivery plan and state "
        "what the Human should approve or revise. Do not create or update work items, runs, files, milestones, "
        "members, capabilities, project status or A2A handoffs, and do not begin delivery. Reply only in the "
        "project conversation.\n\n"
        f"{PROJECT_COLLABORATION_CONTRACT}\n"
        "## Human planning request\n\n"
        f"{request.strip()}"
    )


def build_project_read_only_conversation_task(request: str, *, status: str) -> str:
    """Keep non-running project conversations advisory-only."""

    state = {
        "paused": "paused",
        "waiting": "waiting for review, clarification, or an executable next item",
        "completed": "completed",
    }.get(status, status)
    return (
        f"The project is {state}. Continue the project conversation as the responsible Digital Employee. "
        "Answer questions, explain existing decisions and results, and help the Human assess next steps. "
        "Do not create or update work items, runs, files, milestones, members, capabilities, project status "
        "or A2A handoffs. Do not restart delivery. Reply only in the project conversation.\n\n"
        f"{PROJECT_COLLABORATION_CONTRACT}\n"
        "## Human request\n\n"
        f"{request.strip()}"
    )


def build_project_kickoff_task(transcript: str) -> str:
    """Turn an approved plan into a decision-led first owner execution."""

    return (
        "The Human approved the planning record below. Begin as the project owner by making the first "
        "substantive delivery decision, not by reporting that work has started. Inspect the agreed goal and "
        "available project evidence, create an explicit dependency-aware work-item plan, and execute the next "
        "ready action. Wake a specialist only after that specialist's work item is ready; every handoff must "
        "name the action, cite the relevant context, and define a concrete expected output. Do not ask members "
        "to acknowledge, wait, or provide routine progress updates. Creating or assigning a work item does not "
        "wake its assignee: before ending this turn, send exactly one project A2A task_delegate for each "
        "immediately-ready work item that you move into execution, within the project concurrency limit. Do not "
        "mark a delegated work item in progress without that exact handoff. Write durable decisions and deliverables "
        "to project records and Git. Return to the group only with a professional decision, an artifact, a "
        "material blocker requiring Human judgment, or verified completion evidence.\n\n"
        f"{PROJECT_COLLABORATION_CONTRACT}\n"
        f"{PROJECT_EXECUTION_CONTINUITY_CONTRACT}\n"
        "## Approved planning record\n\n"
        f"{transcript.strip()}"
    )


def build_project_a2a_task(
    request: str,
    *,
    source_name: str = "",
    source_role: str = "",
    target_name: str = "",
    target_role: str = "",
    work_item_title: str = "",
    work_item_snapshot: Mapping[str, object] | None = None,
) -> str:
    """Build the target-only execution task for one exact project A2A turn."""

    normalized_snapshot = normalize_project_work_item_snapshot(work_item_snapshot)
    resolved_work_item_title = (
        _clean(normalized_snapshot.get("title")) if normalized_snapshot else _clean(work_item_title)
    )
    peer_context = "\n".join(
        line
        for line in (
            f"Requester: {_clean(source_name)} — {_clean(source_role)}" if source_name or source_role else "",
            f"Recipient: {_clean(target_name)} — {_clean(target_role)}" if target_name or target_role else "",
            (f"Recipient professional decision lens: {project_professional_lens(target_role)}" if target_role else ""),
            f"Related work item: {resolved_work_item_title}" if resolved_work_item_title else "",
        )
        if line
    )
    snapshot_context = (
        f"\nImmutable work-item snapshot:\n{json.dumps(normalized_snapshot, ensure_ascii=False)}\n"
        if normalized_snapshot
        else ""
    )
    return (
        f"{request.strip()}\n\n---\n{peer_context}{snapshot_context}\n{PROJECT_COLLABORATION_CONTRACT}"
        f"{PROJECT_EXECUTION_CONTINUITY_CONTRACT}"
        "This is one explicit project A2A request from another enabled member. "
        "Respond with the recipient role's independent professional judgment. "
        "Address the requester's actual decision need; do not merely acknowledge, restate, or report progress. "
        "If the request lacks evidence/context, a professional question or an expected result, state exactly what "
        "is missing instead of fabricating work or returning a status acknowledgement. "
        "Handle only this target request and never broadcast or wake unrelated members."
    )


def build_project_owner_batch_task(
    *,
    batch_id: str,
    group_session_id: str,
    replies: Sequence[Mapping[str, object]],
    original_human_request: Mapping[str, object] | None = None,
    work_item_snapshots: Sequence[Mapping[str, object]] = (),
) -> str:
    """Build one decision-oriented owner turn from several participant replies."""

    human_request = None
    if original_human_request:
        human_request = {
            "message_id": bounded_project_text(original_human_request.get("message_id"), 64),
            "content": bounded_project_text(
                original_human_request.get("content"),
                PROJECT_HUMAN_REQUEST_MAX_CHARS,
            ),
        }
    snapshots = [
        normalized
        for snapshot in work_item_snapshots
        if (normalized := normalize_project_work_item_snapshot(snapshot)) is not None
    ]
    payload = json.dumps(
        {
            "owner_batch_id": batch_id,
            "group_session_id": group_session_id,
            "original_human_request": human_request,
            "work_item_snapshots": snapshots,
            "replies": list(replies),
        },
        ensure_ascii=False,
    )
    return (
        "Process this coalesced batch of project member replies in one project-owner turn.\n"
        f"{PROJECT_COLLABORATION_CONTRACT}"
        f"{PROJECT_EXECUTION_CONTINUITY_CONTRACT}"
        "Attribute evidence and conclusions to their source member. Reconcile agreements, "
        "preserve material dissent, and make the next project decision. Update project records "
        "when needed. Do not narrate the inbox as a status digest. Wake a specialist only when an upstream "
        "dependency is ready and you can give them an actionable professional question or deliverable; never "
        "wake anyone for passive awareness or waiting.\n\n"
        f"Batch payload:\n{payload}"
    )
