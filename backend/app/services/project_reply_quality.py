"""Bounded semantic quality guard for project collaboration replies.

The guard intentionally catches only obvious acknowledgements and status-only
updates.  It does not attempt to grade domain correctness; professional
conclusions, evidence, trade-offs, blockers and precise questions remain the
responsibility of the assigned project Agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectReplyQuality:
    needs_correction: bool
    reasons: tuple[str, ...] = ()


_HANDOFF_BLOCKING_REASONS = frozenset(
    {
        "acknowledgement_only",
        "status_only",
        "activity_ledger",
        "internal_narration",
    }
)


_ACKNOWLEDGEMENT_PREFIX = re.compile(
    r"^(?:收到|已收到|好的|好|明白|了解|知悉|没问题|可以|确认收到|"
    r"acknowledged|received|got\s+it|understood|noted|okay|ok)\b",
    re.IGNORECASE,
)
_STATUS_PREFIX = re.compile(
    r"^(?:已完成|处理完成|任务完成|已更新|已同步|正在处理|处理中|继续推进|"
    r"当前进度|进度更新|状态更新|等待(?:中|上游)?|后续(?:会|将)|"
    r"下一步(?:会|将|继续|安排)?|"
    r"completed|done|updated|synced|working\s+on\s+it|in\s+progress|"
    r"status\s+update|progress\s+update|waiting\s+for|will\s+proceed)\b",
    re.IGNORECASE,
)
_PROFESSIONAL_SIGNAL = re.compile(
    r"(?:结论|判断|依据|证据|因为|因此|根因|风险|影响|约束|假设|口径|阈值|"
    r"权衡|取舍|建议|推荐|决策|方案|验收|阻塞|缺少|需要确认|执行人|"
    r"conclusion|judg(?:e)?ment|decision|evidence|because|therefore|root\s+cause|"
    r"risk|impact|constraint|assumption|threshold|trade[ -]?off|recommend|"
    r"acceptance|block(?:er|ed)?|missing|required\s+input|next\s+action|owner)",
    re.IGNORECASE,
)
_CONCRETE_EVIDENCE = re.compile(
    r"(?:`[^`]+`|(?:^|\s)[\w.-]+/[\w./-]+|\bWI-\d+\b|\b(?:run|session|event|commit)\s*"
    r"[#:@-]?[0-9a-f]{6,}\b|\b\d+(?:\.\d+)?%\b|\bHTTP\s+[1-5]\d\d\b)",
    re.IGNORECASE,
)
_STRONG_EVIDENCE = re.compile(
    r"(?:`[^`]+`|(?:^|\s)[\w.-]+/[\w./-]+|\bWI-\d+\b|"
    r"\b(?:run|session|event|commit)\s*[#:@-]?[0-9a-f]{6,}\b)",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"[?？]")
_MECHANICAL_ACTIVITY = re.compile(
    r"(?:已(?:完成|更新|同步|创建|处理|提交|记录)|正在(?:处理|推进)|继续推进|等待(?:中|上游)?|"
    r"(?:have|has|was|were|is|are|been|task|record(?:s)?)?\s*"
    r"(?:completed|updated|synced|created|processed|submitted|recorded|waiting|proceeding))",
    re.IGNORECASE,
)
_INTERNAL_NARRATION_PREFIX = re.compile(
    r"^(?:"
    r"(?:now|next|first|then|finally)\s+(?:(?:i(?:'ll|\s+will)?|let\s+me)\s+)?"
    r"(?:writ(?:e|ing)|creat(?:e|ing)|updat(?:e|ing)|mark(?:ing)?|read(?:ing)?|"
    r"check(?:ing)?|inspect(?:ing)?|analy[sz](?:e|ing)|start(?:ing)?|continu(?:e|ing)|"
    r"respond(?:ing)?|notif(?:y|ying)|call(?:ing)?|us(?:e|ing))\b|"
    r"let\s+me\s+(?:write|create|update|mark|read|check|inspect|analy[sz]e|start|"
    r"continue|respond|notify|call|use)\b|"
    r"(?:现在|接下来|首先|然后|最后)(?:我将|我会|让我)?"
    r"(?:写入|创建|更新|标记|读取|检查|分析|开始|继续|回复|通知|调用|使用)"
    r")",
    re.IGNORECASE,
)


def assess_project_reply(reply: str) -> ProjectReplyQuality:
    """Identify only replies that cannot carry useful professional judgment."""

    text = " ".join(str(reply or "").split()).strip()
    if not text:
        return ProjectReplyQuality(True, ("empty",))

    # Tool calls and project events already preserve execution mechanics.  A
    # response that opens with private step-by-step narration is a rendering
    # leak even when useful professional content appears later in the answer.
    # Correct the prose once without replaying any tools or waking another
    # project member.
    if _INTERNAL_NARRATION_PREFIX.search(text):
        return ProjectReplyQuality(True, ("internal_narration",))

    has_professional_signal = bool(_PROFESSIONAL_SIGNAL.search(text))
    has_concrete_evidence = bool(_CONCRETE_EVIDENCE.search(text))
    has_precise_question = bool(_QUESTION.search(text) and has_professional_signal)
    has_strong_evidence = bool(_STRONG_EVIDENCE.search(text))
    if has_precise_question or has_professional_signal or (has_strong_evidence and len(text) >= 24):
        return ProjectReplyQuality(False)

    reasons: list[str] = []
    if _ACKNOWLEDGEMENT_PREFIX.search(text):
        reasons.append("acknowledgement_only")
    if _STATUS_PREFIX.search(text):
        reasons.append("status_only")
    if len(_MECHANICAL_ACTIVITY.findall(text)) >= 2:
        reasons.append("activity_ledger")

    # Short messages without a conclusion, evidence, trade-off or precise
    # blocker are almost always receipt confirmations even when phrased without
    # one of the explicit prefixes above.  Keep the threshold deliberately low
    # so substantive concise professional judgments are never rewritten.
    if len(text) < 36 and not has_professional_signal and not has_concrete_evidence:
        reasons.append("no_professional_substance")

    return ProjectReplyQuality(bool(reasons), tuple(dict.fromkeys(reasons)))


def project_handoff_rejection_reasons(message: str) -> tuple[str, ...]:
    """Return obvious non-actionable A2A reasons without grading domain work."""

    assessment = assess_project_reply(message)
    return tuple(reason for reason in assessment.reasons if reason in _HANDOFF_BLOCKING_REASONS)


def build_project_reply_correction_prompt(
    reply: str,
    *,
    role: object = "",
    is_owner: bool = False,
) -> str:
    """Request one replacement answer without creating another project turn."""

    from app.services.project_collaboration_prompt import project_professional_lens

    role_text = str(role or "").strip() or "the assigned project role"
    responsibility = (
        "As project owner, reconcile specialist evidence and make the next project decision."
        if is_owner
        else "Stay within this role's professional authority and give the project owner a usable judgment."
    )

    return (
        "Replace your previous answer with the actual professional response for this same request. "
        "Do not acknowledge receipt, announce progress, or describe that you are revising the answer. "
        "Give the role-specific conclusion or artifact first; distinguish evidence from assumptions; "
        "state material risks or trade-offs; and give a concrete recommendation, decision, deliverable, "
        "or one precise unblock question. Cite only evidence that is present in the project context. "
        f"Your immutable project role is: {role_text}. "
        f"Mandatory decision lens: {project_professional_lens(role_text)} "
        f"{responsibility} "
        "This is a single bounded self-correction: do not send messages, wake Agents, or repeat tool actions.\n\n"
        "Previous low-value answer:\n"
        f"{str(reply or '').strip() or '(empty)'}"
    )
