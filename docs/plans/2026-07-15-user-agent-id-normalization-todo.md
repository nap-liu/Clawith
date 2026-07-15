# User / Agent Identity Normalization — Immutable Iteration TODO

> Created: 2026-07-15
> Branch: `feat/user-agent-id-normalization`
> Baseline: `company/main@6bc97d49565667af233cfc19692a77f9f3658a2a`
> Status: implementation started

## Governance

- The twelve numbered workstreams below are the frozen scope approved by the user and independently audited.
- Their count, direction, acceptance boundary, and ordering dependencies must not be removed, merged, split, or redefined during this iteration.
- Only checkbox status and evidence links may change. Any scope change requires explicit user approval and an appended decision record; it must not rewrite the original items.
- Public identity contract: a natural person is addressed only by tenant `user_id`; a digital employee is addressed only by `agent_id`; `display_name` is discovery/display data and never an execution locator.
- The platform owns identity authenticity, tenant isolation, authorization, exact endpoint resolution, idempotency, and truthful errors. The Agent owns target selection, channel selection when multiple valid routes exist, message content, timing, and workflow judgment.
- Do not add a `Contact` identity. Provider, OrgMember, Participant, open_id, unionid, external_id, workspace member ID, and channel account IDs remain internal bindings only.
- No fuzzy/name/first-row execution fallback. Legacy data may migrate only when exact, unique, tenant-scoped, active, and authorized; otherwise it becomes `migration_required`.

## Frozen workstreams

1. [x] **Contract and baseline freeze** — Record forbidden/allowed Agent-visible identity fields; capture current tool/OpenAPI/MCP/Gateway schemas, DB invariants, 3008 SHA/topology, and the executable regression baseline.

2. [x] **Phase-0 platform integrity gate** — Verify Teams/Feishu and other enabled IM ingress; enforce trusted webhook authentication and replay handling; correct org-admin tenant isolation; protect trigger CRUD/internal fields; redact channel secrets; make confirmation intended-actor-bound and approval resolution single-execution/CAS; route file delivery through the same authorization guard as text delivery.

3. [x] **Canonical tenant user and channel bindings** — Reuse `User.id` as the only natural-person `user_id`; create external-only users without synthetic login identities/passwords; model internal channel bindings with `(tenant_id, issuer/installation scope, id_type, full external subject)` uniqueness; use transactional insert-on-conflict-read; never merge by name or unverified contact data.

4. [x] **Relationship persistence normalization** — Persist human relationships as unique `(agent_id, user_id)` and A2A relationships as unique `(agent_id, target_agent_id)` with tenant-safe constraints; retain multi-provider bindings; produce a conflict report before consolidating duplicate legacy member relationships; move human/Agent relationship APIs to explicit `user_id`/`agent_id` contracts.

5. [x] **Exact recipient and delivery resolver** — Introduce one shared internal resolver/guard for platform messages, IM text, files, provider-specific user actions, and current-session replies; resolve only canonical IDs to compatible bindings; reject missing, ambiguous, inactive, cross-tenant, unauthorized, suppressed, or unreachable routes with structured truthful results; never choose the first name match.

6. [x] **Agent tool and context normalization** — Update DB seeder source-of-truth, runtime fallback definitions, descriptions, Agent context, current-conversation identity, and group sender markers so discovery returns exact IDs and all execution uses only `user_id`/`agent_id`; remove long-lived `target_type+target_id`, member/name/username/provider-ID execution parameters; keep current-session implicit file reply semantics.

7. [x] **Session and message identity normalization** — Human P2P sessions/messages carry the canonical user actor; A2A sessions/messages carry canonical agent actors; group messages do not use creator-user placeholders; stop exposing Participant IDs as public identity; preserve conversation-id-based A2A history semantics and prevent pre/post-enrichment session splitting.

8. [x] **Trigger, approval, confirmation, and OKR closure** — Freeze canonical `user_id`/`agent_id` at creation time; trigger/evaluate/approve/confirm/report without later name/provider re-resolution; migrate OKR generic member/owner identity fields and enforce tenant-safe references; ambiguous legacy records fail closed with visible migration status.

9. [x] **A2A, Gateway, MCP, API, and UI parity** — Make native A2A, OpenClaw Gateway poll/send, MCP relationship operations, OpenAPI schemas, REST responses, WebSocket payloads, and relationship UI use the same public IDs; preserve A2A notify/consult/task_delegate/new-conversation/file capabilities; names remain display-only.

10. [x] **Idempotent migration and compatibility closure** — Implement forward migrations/backfills, duplicate/conflict reports, old-user redirect/tombstone handling for audited merges, deterministic reruns, and temporary internal compatibility reads; stop legacy writes, then remove public legacy fields after evidence shows no callers; never delete valid cross-provider bindings.

11. [x] **Automated full regression and concurrency verification** — Cover duplicate names, human/Agent same names, multi-channel people, tenant boundaries, relationship/permission/suppression gates, every supported IM adapter, platform messaging, files, triggers, approval/confirmation, OKR, A2A, Gateway, MCP, session/history, schema consistency, and 100-way identical-ingress concurrency/idempotency.

12. [ ] **3008 real E2E, IM cooperation, and independent final audit** — Run the implementation from this worktree with backend/frontend on one release SHA; validate actual LLM-visible schemas and Agent decisions; complete web/platform/A2A smoke tests and real IM inbound/outbound/file/trigger flow with user assistance; obtain a neutral second-pass audit with no unresolved P0/P1 before proposing production release.

## Baseline evidence already captured

- Local health: `http://127.0.0.1:3008/api/health` returned version `1.10.1` before this worktree was created.
- Runtime schema for local Agent `小智` still exposes `member_name`, `username`, `agent_name`, and `target_type + target_id`.
- Local DB baseline: 1005 OrgMember rows; 98 lack `user_id`; 602 human relationships all map to a user; four duplicate `(agent_id, user_id)` relationship groups require migration handling.
- Existing targeted test baseline: 87 relevant identity/relationship/IM/A2A cases pass; one batch-order-sensitive async DB case passed independently.
- Pre-existing 3008 stack was mixed-source: backend/main `6bc97d4`, frontend worktree `dbd4b99`; it must not be used as final E2E evidence until this worktree owns both sides.

## Implementation evidence

- Branch was fast-forwarded to the actual production baseline `company/main@b45296b6` (H5 speech input). The migration chain is a single head: `speech_recognition_configs -> identity_relationships_v1 -> canonical_chat_senders -> canonical_okr_owners -> canonical_supervision_targets`.
- A fresh read-only production `pg_dump` was restored into the isolated `clawith_identity_e2e` database with the backend in `PROCESS_ROLE=api`. The real production snapshot upgraded from `speech_recognition_configs` to head without stamp/repair SQL; relationship, session, A2A and supervision tenant-invariant queries returned zero violations.
- Production-snapshot migration reported 207 duplicate legacy human relationship rows (deterministically preserved as audit conflicts then consolidated) and 14,454 DingTalk members whose installation cannot be guessed across 22 Agent bot installations. OAuth providers are excluded; unresolved bindings remain lazy/exact at webhook time. Conflict JSON contains only allowlisted IDs/type/count and zero credential-bearing fields.
- Supported IM/Phase-0, current-tenant history, supervision, identity relationship, OKR, Gateway idempotency, binding concurrency, Agent tool contract, WebSocket and production speech regression: 140 passed. The 100-way identical-ingress concurrency contract remains covered.
- Full host backend suite: 1,379 passed, 28 skipped, 13 failed only because macOS lacks the Linux `/data` layout, container cryptography/bash behavior and WeasyPrint libraries. All three affected files passed 35/35 in the production-like Linux backend test image; no identity/tenant/webhook/speech regression failed.
- Frontend identity-contract check, speech worklet tests, TypeScript/Vite production build (8,775 modules), Python compile, fatal Ruff rules and `git diff --check` passed. 3008 frontend and backend were rebuilt from this worktree; health is 200/version 1.10.3.
- 3008 production-snapshot API smoke as the current-tenant platform administrator returned 200 for auth, relationships, exact Agent candidates, tasks, sessions and history. Feishu/Slack authenticated challenge probes returned 200; Feishu/Slack/Teams invalid authentication returned 401; all temporary configs were deleted. No IM connector/Stream/poll task was started.
- Neutral final delta audit verdict is PASS with no unresolved P0/P1 or release blocker. Its only tenantless-owner P2 observation was closed with compatible user/Agent tenant-move guards; the production snapshot clone completed 072-074 downgrade/re-upgrade and remained healthy. The browser runtime currently exposes no browser instance, and real IM inbound/outbound/file/trigger cooperation is intentionally not run while the production-data local stack remains API-only, so workstream 12 stays open.
