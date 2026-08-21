# AI Native Project Remediation Checklist

**Goal:** Close every UI, traceability, session-routing, model, and runtime issue found during the six-Agent end-to-end acceptance run.

**Acceptance rule:** An item is complete only after source checks, automated tests, Docker deployment, and browser verification against a real project. Historical failures remain visible as evidence but must not leave active work or ambiguous links.

## Navigation and layout

- [x] Persist every workspace tab in the URL.
- [x] Persist work graph/board mode, selected work item, work-item detail tab, selected member, selected commit, audit filters, and other meaningful inner selections in the URL.
- [x] Verify refresh and browser back/forward restore the same screen and selection.
- [x] Repair milestone layout and remove the large empty area around related Runs.
- [x] Remove the manual “create Run / advance objective” form from Run history.
- [x] Remove the redundant work-item switcher from work-item detail.
- [x] Replace cockpit Run hero with a concise project objective, summary, member, and progress overview.
- [x] Show completed as well as active work items on the cockpit.
- [x] Populate the cockpit issue column from real blocked/failed/risk events instead of a permanent empty placeholder.
- [x] Remove decorative Human-only notices while preserving server-enforced permissions.
- [x] Remove blank filler below the embedded project group chat.

## Content rendering and internationalization

- [x] Render work-item execution and event content with the standard Markdown renderer.
- [x] Collapse long event content by default and provide an explicit full Markdown preview.
- [x] Render evidence and approval content with Markdown; provide full Markdown preview on hover/focus/click without clipping.
- [x] Translate project Run, work-item, Git, group, and A2A event labels and descriptions through the shared i18n catalog.
- [x] Show the translated event label only; do not repeat the internal event code when a translation exists.
- [x] Make milestone file/diff evidence inspectable through the existing project file/diff viewer.

## Exact session traceability

- [x] Every Run node and Run row must resolve to its exact visible session.
- [x] Persist a message anchor for each Run input/output/event.
- [x] When multiple Runs share one durable session, opening a Run must scroll to the corresponding message and briefly highlight it.
- [x] Apply the same exact-session and anchor behavior to work items, milestones, evidence, A2A events, snapshots, Git, and audit entries.
- [x] Keep exited-member historical sessions readable but non-interactive.

## Member snapshots

- [x] Replace editable raw snapshot JSON with structured, reusable fields and capability controls.
- [x] Keep advanced/raw data available only as a secondary read-only diagnostic view if required.
- [x] Verify every displayed Run in the lineage graph opens the matching anchored session.

## Runtime reliability

- [x] Explain every observed 180-second timeout from provider request, queue, context, and tool timing evidence.
- [x] Add provider-level concurrency control so queue wait does not consume request timeout.
- [x] Add bounded retry only for zero-output/TTFT timeouts; never replay after useful output without recovery semantics.
- [x] Preserve the standard context recovery, compaction, confirmation, durable inbox, and exact ProjectRun state machine.
- [x] Re-run a multi-Agent collaboration flow and verify no active Run remains stranded.

## Tenant models

- [x] Clone the existing local provider configuration to `qwen3.5-plus`.
- [x] Clone the existing local provider configuration to `qwen3.6-plus`.
- [x] Clone the existing local provider configuration to `qwen3.7-plus`.
- [x] Reuse encrypted credential references without exposing or copying plaintext secrets into source code.
- [x] Verify all three models are enabled and visible in the current tenant model selector.
- [x] Perform a health invocation for each model or record an explicit provider response when unavailable.

## Final acceptance

- [x] Run backend project/session/runtime/model tests.
- [x] Run frontend prebuild, TypeScript, and production build.
- [x] Rebuild the local Docker stack.
- [x] Screenshot every workspace tab in light and dark themes where relevant.
- [x] Verify URL persistence, exact anchored sessions, Markdown expansion, i18n, diff viewing, snapshot editing, and model selection in the browser.

## Acceptance evidence

- Real project: `cda3cb3d-142c-48e3-a9a6-b7ffcedd2e86`; status `completed`; 8/8 work items done; 49 durable ProjectRuns; 6 distinct project Agents; 9 Git deliverables; one typed M1 release milestone; no queued or running residue.
- Session integrity: every ProjectRun resolves to an existing project-scoped session and immutable member snapshot; Run links use exact session and message anchors; group, direct A2A, work-item, milestone, Git, snapshot, and audit entry points share the same routing contract.
- Context integrity: project execution uses the standard caller, context recovery, compaction, confirmation, durable inbox, and restart recovery path. The long acceptance project remained below its 85% compaction threshold; zero compactions was therefore expected, not a bypass.
- Timeout remediation: all observed 180-second failures were provider-stream timeouts after tool completion, amplified by overlapping model requests. Provider-scoped concurrency, queue-time exclusion, zero-output TTFT retry, activity renewal, and an absolute hard cap are covered by tests.
- Model verification: `qwen3.5-plus`, `qwen3.6-plus`, and `qwen3.7-plus` were cloned idempotently from the local tenant model without exposing credentials, shown in the tenant selector, and answered live health requests successfully.
- Automated verification: 114 focused backend tests passed; the complete frontend prebuild suite, TypeScript compilation, and production Vite build passed; Docker backend and frontend were rebuilt and reported healthy.
- Browser verification: all 14 workspace tabs loaded without loading/error residue; URL refresh restored audit filters and work-item detail tabs; Monaco displayed a real Git diff; the model selector showed all four Qwen variants; light and dark screenshots were captured in `artifacts/qa-2026-08-21/`.
