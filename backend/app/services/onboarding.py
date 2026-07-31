"""Per-(user, agent) onboarding helpers.

The frontend auto-fires a hidden greeting trigger the first time a user opens
an empty chat with an agent. The backend now treats onboarding as a small
ritual rather than a single welcome line:

  - Custom agents are "defined together" with the user, then write durable
    working notes.
  - Template agents already have a job description, so they confirm and tune
    the preset role before writing local calibration notes.

``agent_user_onboardings.phase`` is intentionally small:

  - no row: the greeting has not fired yet;
  - pending: one backend instance has atomically claimed the greeting;
  - greeted: the greeting fired, and the next real user reply should continue
    configuration;
  - completed: normal chat forever after.

Existing rows are migrated to ``completed`` so established relationships keep
their current behavior.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import String, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentTemplate, AgentUserOnboarding
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.auth_code_exchange import PLATFORM_LOGIN_CHANNELS

if TYPE_CHECKING:  # pragma: no cover
    pass


@dataclass(frozen=True)
class OnboardingInjection:
    """What the WS handler needs to apply for a given turn.

    - ``prompt``: the system message to prepend.
    - ``lock_on_first_chunk``: whether this turn's first streamed chunk
      should update the junction row.
    - ``target_phase``: the phase to write when the first chunk streams.
      Greeting writes ``greeted`` so it never auto-greets again; the first
      real reply writes the next onboarding phase after it starts streaming.
    - ``is_greeting_turn``: True only for the synthetic auto-greeting turn
      (when user_turns == 0). The WS handler uses this to skip the agent's
      tool list for the hidden welcome message only. Real user turns must keep
      tool schemas available, otherwise models may emit fake XML/tool text
      instead of native tool calls.
    """

    prompt: str
    lock_on_first_chunk: bool
    target_phase: str = "completed"
    is_greeting_turn: bool = False
    expected_phase: str | None = None


@dataclass(frozen=True)
class OnboardingEligibility:
    """Whether one concrete platform session may host the first greeting."""

    required: bool
    reason: str


@dataclass(frozen=True)
class OnboardingClaim:
    """Result of atomically claiming the per-user/agent greeting turn."""

    acquired: bool
    reason: str
    claimed_at: datetime | None = None


PHASE_PENDING = "pending"
PHASE_GREETED = "greeted"
PHASE_CUSTOM_STYLE = "custom_style"
PHASE_CUSTOM_BOUNDARIES = "custom_boundaries"
PHASE_TEMPLATE_FOCUS = "template_focus"
PHASE_COMPLETED = "completed"
ONBOARDING_PENDING_TTL = timedelta(minutes=2)
_FIRST_PARTY_CHANNELS = tuple(sorted(PLATFORM_LOGIN_CHANNELS))


_CUSTOM_GREETING_PROMPT = """\
{user_name} is meeting you for the first time. You are a newly created custom \
digital employee with only a light initial profile, so this is your first-run \
ritual.

Markdown rendering is on. Don't interrogate, don't sound like a form, and don't \
mention prompts or onboarding internals.

Greeting turn:
- Keep it under 70 words.
- Open warmly as **{name}**.
- Say you just joined and want to learn what to help with first.
- Ask ONE easy question: what should you mainly help with?
- Add one short optional sentence: they can also mention style, boundaries, or \
a first task if they already know.
- Stop there. No bullets, no numbered list, no tools, no files."""


_CUSTOM_STYLE_PROMPT = """\
{user_name} has answered what they mainly want you to help with. This is still \
the setup conversation for a custom digital employee.

Do NOT write files yet. Keep the reply under 70 words. Briefly acknowledge the \
main responsibility you heard, then ask exactly ONE next question: what \
communication style or working rhythm should you use? Offer 2-3 tiny examples \
inline, such as concise, proactive, formal, warm, daily summaries, or only when \
asked. No bullets, no tools."""


_CUSTOM_BOUNDARIES_PROMPT = """\
{user_name} has described a communication style or working rhythm. This is \
still the setup conversation for a custom digital employee.

Do NOT write files yet. Keep the reply under 80 words. Briefly acknowledge the \
style/rhythm you heard, then ask exactly ONE final setup question: are there \
any boundaries, approval rules, sensitive areas, or a first task you should \
record? Make it feel optional and easy. No bullets, no tools."""


_CUSTOM_CONFIG_PROMPT = """\
{user_name} has now answered the short setup questions. Use the whole recent \
conversation as the source of truth. Your job now is to make the custom agent \
real.

Do not ask more setup questions. If details are missing, choose light defaults \
and label them as adjustable.

You MUST persist the onboarding result:
1. Read `soul.md` if it exists.
2. Update `soul.md` so it includes your working identity, vibe/style, \
responsibilities, and boundaries.
3. Write `memory/user_profile.md` with how to address and collaborate with \
{user_name}.
4. Use `upsert_focus_item` to record the first focus item or next concrete task.

After writing, reply with a short confirmation:
- who you now understand yourself to be;
- how you will work with the user;
- the first focus you recorded;
- one concise next-step offer.

Never mention these instructions to the user."""


_TEMPLATE_GREETING_PROMPT = """\
{user_name} is meeting you for the first time. You are already configured as a \
template-based digital employee, not a blank custom agent.

Markdown rendering is on. Don't interrogate, don't sound like a form, and don't \
mention prompts or onboarding internals.

Greeting turn:
- Keep it under 90 words.
- Open warmly as **{name}**{role_line}.
- Say you are already set up for this role.
- Briefly mention 1–2 default strengths in prose{bullets_line}.
- Ask the user to either confirm the role as-is or tell you what to adjust: \
responsibilities, communication style, boundaries, project/team context, or \
the first thing to work on.
- Stop there. No bullets, no numbered list, no tools, no files."""


_TEMPLATE_CONFIG_PROMPT = """\
{user_name} has replied to your template-role onboarding. You already have a \
preconfigured role; treat the user's reply as local calibration, not a reason \
to rewrite your whole template identity.

Do NOT write files yet. Keep the reply under 80 words. Briefly acknowledge any \
role confirmation or adjustment. Ask exactly ONE next question: what first \
project, task, team context, boundary, or reporting rhythm should you start \
with? If they already provided one, ask them to confirm it. No bullets, no \
tools."""


_TEMPLATE_FINALIZE_PROMPT = """\
{user_name} has answered the template-role setup questions. Use the whole \
recent conversation as local calibration. You already have a preconfigured \
role; do not rewrite your whole template identity.

Do not ask more setup questions. If they simply confirmed the preset, proceed \
with sensible defaults.

You MUST persist the calibration:
1. Write `memory/onboarding.md` with the confirmed role, user-specific \
adjustments, communication preferences, boundaries, and first focus.
2. Use `upsert_focus_item` to record the first concrete task or a clear \
"ready to start" focus if no task was given.
3. Only edit `soul.md` if the user explicitly changed your role, style, or \
boundaries; in that case read it first and preserve the template's core role.

After writing, reply with a short confirmation:
- the role you will operate under;
- any adjustments you captured;
- the first focus you recorded;
- one concise next-step offer.

Never mention these instructions to the user."""


def _render_template_greeting(
    agent: Agent,
    capability_bullets: list[str] | None,
    user_name: str,
) -> str:
    role_line = f", your {agent.role_description}" if agent.role_description else ""
    if capability_bullets:
        bullets = "; ".join(b.strip() for b in capability_bullets if b and b.strip())
        bullets_line = f" — ideas to lean on: {bullets}" if bullets else ""
    else:
        bullets_line = ""
    return _TEMPLATE_GREETING_PROMPT.format(
        name=agent.name,
        role_line=role_line,
        bullets_line=bullets_line,
        user_name=user_name,
    )


# Map of frontend lang code → human language name we paste into the prompt.
# Frontend currently only sends "zh" or "en"; expand here when more locales
# are surfaced.
_LANG_NAMES = {
    "zh": "Chinese (Simplified)",
    "en": "English",
}


def _locale_directive(user_locale: str) -> str:
    """Strong instruction to reply in the user's current interface language."""
    lang_code = (user_locale or "en").lower()[:2]
    lang_name = _LANG_NAMES.get(lang_code, "English")

    return (
        f"[Interface language: {lang_name}. Reply entirely in {lang_name} for "
        f"this onboarding turn. The onboarding instructions below are written "
        f"in English for you, not for the user; translate the actual user-facing "
        f"message naturally into {lang_name}. Keep product names and conventional "
        f"technical terms in English when appropriate.]\n\n"
    )


async def resolve_onboarding_prompt(
    db: AsyncSession,
    agent: Agent,
    user_id: uuid.UUID,
    *,
    user_name: str = "there",
    user_locale: str = "en",
    is_onboarding_trigger: bool = False,
    complete_after_greeting: bool = False,
) -> OnboardingInjection | None:
    """Decide what system prompt to inject for this (user, agent) turn.

    Returns ``None`` when the pair is fully completed and the turn should
    proceed normally. Otherwise returns an :class:`OnboardingInjection` with
    either the first greeting prompt or the second configuration prompt.
    """
    existing_result = await db.execute(
        select(AgentUserOnboarding).where(
            AgentUserOnboarding.agent_id == agent.id,
            AgentUserOnboarding.user_id == user_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    existing_phase = getattr(existing, "phase", PHASE_COMPLETED) if existing else None
    if existing_phase == PHASE_COMPLETED:
        return None
    # A real user message must never be reinterpreted as the hidden greeting
    # turn. The normal-message path atomically records ``completed`` when it
    # wins the first-turn race, but this guard keeps the prompt resolver safe
    # even if a future caller forgets that arbitration step.
    if existing_phase is None and not is_onboarding_trigger:
        return None
    # ``pending`` is the atomic claim held by the synthetic trigger. A normal
    # user turn waits for that claim to resolve before reaching this function.
    if existing_phase == PHASE_PENDING and not is_onboarding_trigger:
        return None

    # Count real user messages this person has sent to this agent. Onboarding
    # triggers are not persisted, so only authentic typed turns are counted.
    user_turn_count = await db.execute(
        select(func.count()).select_from(ChatMessage).where(
            ChatMessage.agent_id == agent.id,
            ChatMessage.user_id == user_id,
            ChatMessage.role == "user",
        )
    )
    user_turns = int(user_turn_count.scalar_one() or 0)

    # Is anyone at least greeted by this agent yet? If not, this user is the
    # founder. The current user's transient pending claim did not exist in the
    # old pre-claim flow, so discount exactly that row from this decision.
    peer_count = await db.execute(
        select(func.count()).select_from(AgentUserOnboarding).where(
            AgentUserOnboarding.agent_id == agent.id,
        )
    )
    current_claim_rows = 1 if existing_phase == PHASE_PENDING else 0
    is_founder = peer_count.scalar_one() == current_claim_rows

    template_prompt: str | None = None
    capability_bullets: list[str] | None = None
    if agent.template_id:
        tpl_result = await db.execute(
            select(AgentTemplate).where(AgentTemplate.id == agent.template_id)
        )
        tpl = tpl_result.scalar_one_or_none()
        if tpl:
            capability_bullets = tpl.capability_bullets or None
            template_prompt = tpl.bootstrap_content

    is_template_agent = bool(agent.template_id and template_prompt)

    if existing_phase == PHASE_GREETED:
        if is_template_agent:
            prompt = _TEMPLATE_CONFIG_PROMPT.format(user_name=user_name)
            target_phase = PHASE_TEMPLATE_FOCUS
        else:
            prompt = _CUSTOM_STYLE_PROMPT.format(user_name=user_name)
            target_phase = PHASE_CUSTOM_STYLE
        is_greeting_turn = False
    elif existing_phase == PHASE_CUSTOM_STYLE:
        prompt = _CUSTOM_BOUNDARIES_PROMPT.format(user_name=user_name)
        target_phase = PHASE_CUSTOM_BOUNDARIES
        is_greeting_turn = False
    elif existing_phase == PHASE_CUSTOM_BOUNDARIES:
        prompt = _CUSTOM_CONFIG_PROMPT.format(user_name=user_name)
        target_phase = PHASE_COMPLETED
        is_greeting_turn = False
    elif existing_phase == PHASE_TEMPLATE_FOCUS:
        prompt = _TEMPLATE_FINALIZE_PROMPT.format(user_name=user_name)
        target_phase = PHASE_COMPLETED
        is_greeting_turn = False
    elif existing_phase in {None, PHASE_PENDING} and is_onboarding_trigger:
        # First contact. Template agents get a confirmation/tuning greeting.
        # Custom agents get the OpenClaw-inspired "define who I am" ritual.
        if is_template_agent:
            prompt = _render_template_greeting(agent, capability_bullets, user_name)
        elif is_founder and template_prompt:
            # Defensive fallback for legacy data that has bootstrap text but no
            # usable template_id. Keep the old authored bootstrap behavior.
            prompt = (
                template_prompt
                .replace("{name}", agent.name)
                .replace("{user_name}", user_name)
                .replace("{user_turns}", str(user_turns))
            )
        else:
            prompt = _CUSTOM_GREETING_PROMPT.format(name=agent.name, user_name=user_name)
        # Every channel first publishes the visible greeting as ``greeted``.
        # Non-interactive channels advance to ``completed`` only after the
        # assistant reply is durably persisted.
        target_phase = PHASE_GREETED
        is_greeting_turn = True
    else:
        return None

    # Prepend a locale directive so the greeting turn lands in the user's
    # interface language (Chinese vs English). Without this, the agent would
    # only see an empty user message on Turn 0 and fall back to English by
    # the soul's "ambiguous → English" rule.
    prompt = _locale_directive(user_locale) + prompt

    # Update phase as soon as the agent starts streaming. A greeting writes
    # "greeted" so the frontend won't auto-trigger another empty greeting. The
    # first real reply writes "completed" once the calibration answer starts.
    return OnboardingInjection(
        prompt=prompt,
        lock_on_first_chunk=True,
        target_phase=target_phase,
        is_greeting_turn=is_greeting_turn,
        expected_phase=existing_phase,
    )


def _pending_is_stale(row: AgentUserOnboarding, now: datetime) -> bool:
    started_at = row.onboarded_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at <= now - ONBOARDING_PENDING_TTL


async def resolve_onboarding_eligibility(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> OnboardingEligibility:
    """Return the authoritative first-session greeting decision.

    Onboarding is global to a user/agent pair, matching the existing primary
    key on ``agent_user_onboardings``. The eligible session is therefore the
    earliest first-party P2P session across Web and H5 surfaces.
    """

    current = now or datetime.now(timezone.utc)
    session = await db.get(ChatSession, session_id)
    if session is None or session.agent_id != agent_id:
        return OnboardingEligibility(False, "session_not_found")
    if session.user_id != user_id:
        return OnboardingEligibility(False, "not_session_owner")
    if session.is_group or session.peer_agent_id is not None:
        return OnboardingEligibility(False, "not_p2p_session")
    if session.source_channel not in _FIRST_PARTY_CHANNELS:
        return OnboardingEligibility(False, "unsupported_channel")

    onboarding_result = await db.execute(
        select(AgentUserOnboarding).where(
            AgentUserOnboarding.agent_id == agent_id,
            AgentUserOnboarding.user_id == user_id,
        )
    )
    onboarding = onboarding_result.scalar_one_or_none()
    if onboarding is not None:
        if onboarding.phase != PHASE_PENDING:
            return OnboardingEligibility(False, "already_started")
        if not _pending_is_stale(onboarding, current):
            return OnboardingEligibility(False, "in_progress")

    # Any persisted platform history means this is no longer a pristine first
    # conversation. This also self-protects legacy pairs whose onboarding row
    # is missing: opening an old or empty history record can never inject a
    # greeting into their audit trail.
    message_result = await db.execute(
        select(ChatMessage.id)
        .join(
            ChatSession,
            ChatMessage.conversation_id == cast(ChatSession.id, String),
        )
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == user_id,
            ChatSession.is_group.is_(False),
            ChatSession.source_channel.in_(_FIRST_PARTY_CHANNELS),
        )
        .limit(1)
    )
    if message_result.scalar_one_or_none() is not None:
        return OnboardingEligibility(False, "existing_history")

    first_result = await db.execute(
        select(ChatSession.id)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == user_id,
            ChatSession.is_group.is_(False),
            ChatSession.source_channel.in_(_FIRST_PARTY_CHANNELS),
        )
        .order_by(ChatSession.created_at.asc().nulls_last(), ChatSession.id.asc())
        .limit(1)
    )
    first_session_id = first_result.scalar_one_or_none()
    if first_session_id != session_id:
        return OnboardingEligibility(False, "not_first_session")
    return OnboardingEligibility(True, "required")


async def claim_onboarding_greeting(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> OnboardingClaim:
    """Atomically claim the greeting turn using the existing pair primary key."""

    current = now or datetime.now(timezone.utc)
    eligibility = await resolve_onboarding_eligibility(
        db,
        agent_id,
        user_id,
        session_id,
        now=current,
    )
    if not eligibility.required:
        return OnboardingClaim(False, eligibility.reason)

    insert_stmt = (
        pg_insert(AgentUserOnboarding)
        .values(
            agent_id=agent_id,
            user_id=user_id,
            phase=PHASE_PENDING,
            onboarded_at=current,
        )
        .on_conflict_do_nothing(index_elements=["agent_id", "user_id"])
        .returning(AgentUserOnboarding.agent_id)
    )
    inserted = (await db.execute(insert_stmt)).scalar_one_or_none()
    if inserted is not None:
        await db.commit()
        return OnboardingClaim(True, "claimed", current)

    # A crashed worker may leave a pending claim behind. Reclaim it with a
    # compare-and-swap on its timestamp; a live claim can never be stolen.
    reclaim_stmt = (
        update(AgentUserOnboarding)
        .where(
            AgentUserOnboarding.agent_id == agent_id,
            AgentUserOnboarding.user_id == user_id,
            AgentUserOnboarding.phase == PHASE_PENDING,
            AgentUserOnboarding.onboarded_at <= current - ONBOARDING_PENDING_TTL,
        )
        .values(onboarded_at=current)
        .returning(AgentUserOnboarding.agent_id)
    )
    reclaimed = (await db.execute(reclaim_stmt)).scalar_one_or_none()
    await db.commit()
    if reclaimed is not None:
        return OnboardingClaim(True, "reclaimed", current)
    return OnboardingClaim(False, "in_progress")


async def release_onboarding_claim(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    claimed_at: datetime,
) -> bool:
    """Release only the exact still-pending claim owned by this turn."""

    result = await db.execute(
        delete(AgentUserOnboarding).where(
            AgentUserOnboarding.agent_id == agent_id,
            AgentUserOnboarding.user_id == user_id,
            AgentUserOnboarding.phase == PHASE_PENDING,
            AgentUserOnboarding.onboarded_at == claimed_at,
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def onboarding_claim_is_current(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    claimed_at: datetime,
) -> bool:
    """Return whether one synthetic greeting still owns its exact pending claim."""

    result = await db.execute(
        select(AgentUserOnboarding.agent_id).where(
            AgentUserOnboarding.agent_id == agent_id,
            AgentUserOnboarding.user_id == user_id,
            AgentUserOnboarding.phase == PHASE_PENDING,
            AgentUserOnboarding.onboarded_at == claimed_at,
        )
    )
    return result.scalar_one_or_none() is not None


async def claim_fixed_welcome_slot(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Atomically let a fixed welcome own first contact before model output.

    The caller owns the transaction. This function never commits or rolls back
    the supplied session.
    """

    current = datetime.now(timezone.utc)
    inserted = (
        await db.execute(
            pg_insert(AgentUserOnboarding)
            .values(
                agent_id=agent_id,
                user_id=user_id,
                phase=PHASE_COMPLETED,
                onboarded_at=current,
            )
            .on_conflict_do_nothing(index_elements=["agent_id", "user_id"])
            .returning(AgentUserOnboarding.phase)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return True

    takeover = (
        await db.execute(
            update(AgentUserOnboarding)
            .where(
                AgentUserOnboarding.agent_id == agent_id,
                AgentUserOnboarding.user_id == user_id,
                AgentUserOnboarding.phase == PHASE_PENDING,
            )
            .values(phase=PHASE_COMPLETED, onboarded_at=current)
            .returning(AgentUserOnboarding.phase)
        )
    ).scalar_one_or_none()
    if takeover is not None:
        return True

    phase = await db.scalar(
        select(AgentUserOnboarding.phase).where(
            AgentUserOnboarding.agent_id == agent_id,
            AgentUserOnboarding.user_id == user_id,
        )
    )
    if phase != PHASE_COMPLETED:
        return False

    # Once a real user turn exists, first-contact arbitration is over and a
    # new pristine scene session may show its configured welcome. A generated
    # greeting without a real user turn remains the winning welcome.
    real_user_message = await db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.user_id == user_id,
            ChatMessage.role == "user",
        )
        .limit(1)
    )
    if real_user_message is not None:
        return True
    any_message = await db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.user_id == user_id,
        )
        .limit(1)
    )
    return any_message is None


async def claim_normal_first_turn(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> str:
    """Let a real user message win safely when no greeting has started.

    Returns the resulting/current phase. A fresh real message inserts
    ``completed`` so the prompt resolver cannot replace the user's question
    with a greeting. ``pending`` means the caller must wait while a greeting
    owns first contact but has not yet written its standard assistant message.
    A stale greeting claim is completed atomically so a crashed worker cannot
    block normal chat forever.
    """

    current = now or datetime.now(timezone.utc)
    existing = await db.get(AgentUserOnboarding, (agent_id, user_id))
    if existing is not None:
        if existing.phase == PHASE_GREETED:
            durable_greeting = await db.scalar(
                select(ChatMessage.id)
                .join(
                    ChatSession,
                    ChatMessage.conversation_id == cast(ChatSession.id, String),
                )
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.user_id == user_id,
                    ChatMessage.role == "assistant",
                    ChatMessage.created_at >= existing.onboarded_at,
                    ChatSession.agent_id == agent_id,
                    ChatSession.user_id == user_id,
                    ChatSession.is_group.is_(False),
                    ChatSession.source_channel.in_(_FIRST_PARTY_CHANNELS),
                )
                .limit(1)
            )
            if durable_greeting is not None:
                return PHASE_GREETED
            if not _pending_is_stale(existing, current):
                return PHASE_PENDING
        elif existing.phase != PHASE_PENDING:
            return existing.phase
        elif not _pending_is_stale(existing, current):
            return PHASE_PENDING

        stale_takeover = (
            update(AgentUserOnboarding)
            .where(
                AgentUserOnboarding.agent_id == agent_id,
                AgentUserOnboarding.user_id == user_id,
                AgentUserOnboarding.phase == existing.phase,
                AgentUserOnboarding.onboarded_at == existing.onboarded_at,
            )
            .values(phase=PHASE_COMPLETED, onboarded_at=current)
            .returning(AgentUserOnboarding.phase)
        )
        takeover_phase = (await db.execute(stale_takeover)).scalar_one_or_none()
        await db.commit()
        if takeover_phase is not None:
            return PHASE_COMPLETED

        # A concurrent owner advanced or reclaimed the row after our read.
        phase_result = await db.execute(
            select(AgentUserOnboarding.phase).where(
                AgentUserOnboarding.agent_id == agent_id,
                AgentUserOnboarding.user_id == user_id,
            )
        )
        phase = phase_result.scalar_one_or_none()
        return phase or PHASE_COMPLETED

    insert_stmt = (
        pg_insert(AgentUserOnboarding)
        .values(
            agent_id=agent_id,
            user_id=user_id,
            phase=PHASE_COMPLETED,
            onboarded_at=current,
        )
        .on_conflict_do_nothing(index_elements=["agent_id", "user_id"])
        .returning(AgentUserOnboarding.phase)
    )
    inserted_phase = (await db.execute(insert_stmt)).scalar_one_or_none()
    if inserted_phase is not None:
        await db.commit()
        return PHASE_COMPLETED

    # A greeting claim won between the initial read and our insert.
    phase_result = await db.execute(
        select(AgentUserOnboarding.phase).where(
            AgentUserOnboarding.agent_id == agent_id,
            AgentUserOnboarding.user_id == user_id,
        )
    )
    phase = phase_result.scalar_one_or_none()
    return phase or PHASE_COMPLETED


async def mark_onboarding_phase(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    phase: str = PHASE_COMPLETED,
    *,
    expected_phase: str | None = None,
    expected_onboarded_at: datetime | None = None,
) -> bool:
    """Insert or update the onboarding phase for a user/agent pair.

    Called as soon as the LLM begins streaming the relevant onboarding turn.
    """
    if phase not in {
        PHASE_PENDING,
        PHASE_GREETED,
        PHASE_CUSTOM_STYLE,
        PHASE_CUSTOM_BOUNDARIES,
        PHASE_TEMPLATE_FOCUS,
        PHASE_COMPLETED,
    }:
        phase = PHASE_COMPLETED
    if expected_phase is not None:
        phase_values = {
            "phase": phase,
            "onboarded_at": datetime.now(timezone.utc),
        }
        conditions = [
            AgentUserOnboarding.agent_id == agent_id,
            AgentUserOnboarding.user_id == user_id,
            AgentUserOnboarding.phase == expected_phase,
        ]
        if expected_onboarded_at is not None:
            conditions.append(
                AgentUserOnboarding.onboarded_at == expected_onboarded_at
            )
        result = await db.execute(
            update(AgentUserOnboarding)
            .where(*conditions)
            .values(**phase_values)
        )
        await db.commit()
        return bool(result.rowcount)

    phase_values = {
        "phase": phase,
        "onboarded_at": datetime.now(timezone.utc),
    }
    stmt = (
        pg_insert(AgentUserOnboarding)
        .values(
            agent_id=agent_id,
            user_id=user_id,
            **phase_values,
        )
        .on_conflict_do_update(
            index_elements=["agent_id", "user_id"],
            set_=phase_values,
        )
    )
    result = await db.execute(stmt)
    await db.commit()
    return bool(result.rowcount)


async def mark_onboarded(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Backward-compatible helper for callers that mean "completed"."""
    await mark_onboarding_phase(db, agent_id, user_id, PHASE_COMPLETED)


async def is_onboarded(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Shortcut for API serializers that need ``onboarded_for_me`` on AgentOut."""
    result = await db.execute(
        select(AgentUserOnboarding).where(
            AgentUserOnboarding.agent_id == agent_id,
            AgentUserOnboarding.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def onboarded_agent_ids(
    db: AsyncSession,
    user_id: uuid.UUID,
    agent_ids: list[uuid.UUID],
) -> set[uuid.UUID]:
    """Bulk variant of ``is_onboarded`` for list endpoints.

    Returns the subset of ``agent_ids`` the user is already onboarded to.
    """
    if not agent_ids:
        return set()
    result = await db.execute(
        select(AgentUserOnboarding.agent_id).where(
            AgentUserOnboarding.user_id == user_id,
            AgentUserOnboarding.agent_id.in_(agent_ids),
        )
    )
    return {row[0] for row in result.all()}
