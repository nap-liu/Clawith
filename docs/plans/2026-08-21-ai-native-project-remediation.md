# AI Native Project Remediation Checklist

**Goal:** Close every UI, traceability, session-routing, model, and runtime issue found during real multi-Agent end-to-end acceptance runs across eight materially different business domains.

**Acceptance rule:** An item is complete only after source checks, automated tests, Docker deployment, and browser verification against a real project. Historical failures remain visible as evidence but must not leave active work or ambiguous links.

## Eight-domain real-project acceptance matrix

The previous six-Agent release-control project remains a regression baseline, but it is not sufficient for final acceptance. Final acceptance requires eight new projects whose inputs, collaboration topology, outputs, review gates, and evidence are materially different. A project only passes when its负责人 drives the work from the initial conversation through final acceptance; manually seeding rows in the database is not acceptance evidence.

| #   | Domain and public case baseline                                                                                       | Realistic inputs                                                                                                                 | Required roles and collaboration mode                                                                                                                                                                                | Required outputs and evidence                                                                                                                                                        | Domain-specific acceptance gate                                                                                                                                                                     | Status                                                                                                            |
| --- | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| 1   | Software development and incident readiness — Google SRE incident-response case studies and PagerDuty operating model | Product brief, incident scenarios, SLOs, repository, UI constraints, test matrix                                                 | Product owner as负责人; UI/UX, frontend, backend, QA, and operations work through a dependency DAG, code review, defect loop, release gate, and Git merge                                                            | Working application, architecture and UX specifications, source code, automated tests, deployment/runbook, release note, Git commits, rollback point, exact Run/session/A2A evidence | Application starts in Docker; functional, dependency, offline, security, and rollback checks pass; milestone references every work item, Run, session, file, commit, and approval                   | Completed — `PulseOps` project `04387e1c-a2d2-4523-9480-4001b6afc54b`; 5/5 work items, 12/12 Runs, Git `54730054` |
| 2   | Marketing launch — New Balance Fresh Foam launch case                                                                 | Product brief, target audiences, channel constraints, creative assets, launch calendar, KPI definitions, measurement assumptions | Marketing负责人 coordinates audience research, creative, copy, channel operations, analyst, and brand/legal review; creative review is iterative while channel setup and measurement run in parallel                 | Positioning, audience segments, campaign concepts, copy variants, content calendar, channel plan, UTM taxonomy, experiment plan, launch checklist, post-launch report                | Every asset has owner, channel, audience, version, approval, and measurement link; claims and brand review pass before scheduling; KPI report reconciles to source data                             | Runtime completed — `fe7c78a6-f49e-48d9-b34e-948961b41645`; 9/9 items, strict semantic/lineage gate open             |
| 3   | Data analysis — Google Analytics Merchandise Store real ecommerce data                                                | Event/ecommerce extract, data dictionary, business questions, time window, quality rules                                         | Analytics负责人 coordinates data engineer, analyst, BI designer, business stakeholder, and data QA; ingestion, profiling, metric design, dashboarding, and reconciliation use a staged data pipeline                 | Reproducible queries/notebook, cleaned dataset manifest, metric dictionary, funnel/cohort analysis, dashboard specification, insight memo, data-quality report                       | Metrics reproduce from the supplied extract; missing/duplicate/outlier handling is documented; business conclusions cite queries and data versions                                                  | Runtime completed — `37e7eaf9-46b8-4bdc-9b2c-d50a72424d85`; 6/7 items, strict completion/lineage gate open         |
| 4   | Human resources and recruiting — GitLab public hiring and scorecard process                                           | Approved headcount, job description, competency rubric, synthetic candidate packets, interview availability, privacy constraints | Hiring manager as负责人; recruiter, sourcer, interviewers, HRBP, and compliance use sequential stage gates, independent scorecards, debrief, and confidential access boundaries                                      | Hiring plan, JD, sourcing brief, interview kit, structured scorecards, scheduling plan, debrief record, decision rationale, candidate communication templates, audit log             | No candidate advances without required independent scorecards; confidential artifacts remain scoped; final decision is explainable and bias/privacy checks are recorded                             | Runtime completed — `234e0e29-d829-4d50-97da-9a4eb7024bd5`; 6/6 items                                              |
| 5   | Business operations — NYC 311 service-request operations                                                              | Public 311 request sample, service taxonomy, borough/agency ownership, priority and SLA rules, shift calendar                    | Operations负责人 coordinates dispatcher, data analyst, field-operations leads, quality reviewer, and public communications; work arrives as an event queue with routing, reassignment, escalation, and shift handoff | Classified and prioritized queue, routing plan, SLA dashboard, backlog/risk report, field work orders, handoff log, exception playbook, public status summary                        | Every request has traceable classification, owner, SLA clock, handoff, resolution evidence, and reopen path; aggregate counts reconcile to the input sample                                         | Runtime completed — `ccee7cae-39c0-4615-b050-2704636149be`; 7/7 items, 2 historical failed Runs retained           |
| 6   | Customer service — Software AG ticket-resolution case on Jira Service Management                                      | Multichannel ticket sample, customer/account context, product/service catalog, SLA and escalation policy, knowledge base         | Support负责人 coordinates Tier 1, Tier 2, product specialist, quality reviewer, and knowledge manager; work uses SLA queues, escalation, linked engineering tasks, customer replies, and knowledge reuse             | Triage queue, response drafts, escalation links, troubleshooting record, resolution summary, knowledge articles, SLA/CSAT report, reopen handling                                    | Priority and SLA are correct; escalated tickets link to the exact specialist session/work item; customer-visible replies exclude internal notes; resolved cases create or update reusable knowledge | Runtime completed — `74a86053-9b98-4587-8c7c-58f6c2cea993`; 5/5 items, 12 historical failed Runs retained          |
| 7   | Finance and procurement — AWS/FinOps multi-cloud cost-governance case                                                 | FOCUS-like cost-and-usage extract, account/tag mapping, budgets, contracts/pricing, anomaly thresholds, ownership map            | FinOps负责人 coordinates finance analyst, procurement, cloud architect, service owners, and auditor; anomaly events trigger investigation, technical action, savings validation, and financial/procurement approval  | Normalized cost model, anomaly report, showback/chargeback allocation, savings opportunities, commitment-purchase proposal, approval record, forecast, realized-savings ledger       | Allocations reconcile to source totals; each recommendation states cost, risk, owner, evidence, approval, and realized outcome; purchase commitments require human approval                         | Runtime completed — `07291247-4986-4d48-b006-0f64d650e9c2`; 6/6 items, 1 historical failed Run retained            |
| 8   | Compliance and risk research — NIST CSF 2.0 Organizational Profile                                                    | Current/target CSF profile, policies, system inventory, control evidence, risk appetite, applicable requirements                 | Compliance负责人 coordinates security architect, system owners, risk manager, legal reviewer, and independent auditor; evidence-request loops feed current/target gap analysis and remediation approval              | Current/target profile, evidence index, gap and risk register, control mapping, remediation roadmap, exception record, executive summary, audit-ready evidence pack                  | No control is marked satisfied without evidence; gaps map to owners and deadlines; exceptions include approver and expiry; final profile is reproducible from versioned source evidence             | Runtime completed — `64db50c9-aa45-47f2-bca9-afeedd88e495`; 7/7 items, strict semantic/lineage gate open           |

### Cross-domain completion rules

Scope update (2026-08-26): the original requirement to rebuild and re-accept all
eight historical projects was replaced by the user's request for one fresh,
real cross-domain project. Historical projects remain regression evidence; they
are not open development tasks. The final project passed planning approval,
role-specific project Agents, directed A2A work, durable evidence, Git delivery,
and source-Agent isolation.

- [x] The final project begins with a user-to-负责人 planning conversation, records the agreed scope, and is explicitly approved before autonomous execution starts.
- [x] The final project uses role-appropriate project Agents; no generic “万能研发 Agent” substitutes for a business role.
- [x] Collaboration uses targeted durable work and exact A2A lineage rather than broadcast fan-out.
- [x] Every work item, Run, A2A message, group message, file, Git commit, milestone, approval, and audit event resolves to the exact session and message anchor that produced it.
- [x] All project sessions use the same standard Web Chat renderer, context construction, compaction, recovery, tool rendering, and durable history path.
- [x] Removed Agents remain visible in history but cannot receive new messages or perform project actions.
- [x] Lists use the shared, business-agnostic, i18n-enabled pagination component and preserve page/filter/selection state in the URL.
- [x] Light/dark themes use shared tokens; project workspace tabs and relevant detail states received browser review.
- [x] Failed checks produced remediation items, source fixes, Docker verification, and fresh-project reruns.
- [x] Final product sign-off uses the user-approved fresh cross-domain project: no queued/running residue, unresolved blocker, ambiguous session link, or missing Git evidence. The former eight-project completion requirement is superseded.

### Public baselines

- Google SRE incident response and postmortem cases: https://sre.google/workbook/incident-response/ and https://sre.google/workbook/postmortem-culture/
- New Balance Fresh Foam product-launch case: https://www.thinkwithgoogle.com/_qs/documents/458/new-balance-sets-record-with-fresh-foam-launch.pdf
- Google Analytics Merchandise Store demo data: https://support.google.com/analytics/answer/6367342
- GitLab hiring handbook and interview scorecards: https://handbook.gitlab.com/handbook/hiring/ and https://handbook.gitlab.com/handbook/engineering/workflow/hiring/
- NYC 311 service-request dataset: https://data.cityofnewyork.us/Social-Services/311-Service-Requests-from-2020-to-Present/erm2-nwe9
- Software AG service-management case: https://www.atlassian.com/customers/software-ag-itsm
- AWS multi-cloud FinOps case and FinOps public-sector playbook: https://aws.amazon.com/blogs/aws-cloud-financial-management/automating-multi%E2%80%91cloud-cost-management-at-scale-a2as-finops-platform-powered-by-aws/ and https://www.finops.org/wp-content/uploads/2022/10/FinOps-Foundation_US-Gov-Playbook.pdf
- NIST CSF 2.0 Organizational Profiles and community resources: https://www.nist.gov/cyberframework/profiles and https://www.nist.gov/cyberframework/csf-20-resources-0

## Navigation and layout

- [x] Persist every workspace tab in the URL.
- [x] Persist work graph/board mode, selected work item, work-item detail tab, selected member, selected commit, audit filters, and other meaningful inner selections in the URL.
- [x] Verify refresh and browser back/forward restore the same screen and selection.
- [x] Replace the milestone related-Run full-screen modal with the standard right-side Drawer and shared Pagination.
- [x] Remove the manual “create Run / advance objective” form from Run history.
- [x] Remove the redundant work-item switcher from work-item detail.
- [x] Replace cockpit Run hero with a concise project objective, summary, member, and progress overview.
- [x] Show completed as well as active work items on the cockpit.
- [x] Populate the cockpit issue column from real blocked/failed/risk events instead of a permanent empty placeholder.
- [x] Remove decorative Human-only notices while preserving server-enforced permissions.
- [x] Remove blank filler below the embedded project group chat.
- [x] Keep the project navigation fixed while only the active workspace panel scrolls.
- [x] Keep audit filters and table headers fixed while long audit results scroll.
- [x] Extract the proven main-branch pagination behavior into the shared, business-agnostic, i18n `Pagination` component.
- [x] Replace duplicated pagination in project lists, templates, published pages, visitor history, users, companies, and invitation codes.
- [x] Paginate dashboard work items, project risks, Run history, audit events, Git commits, milestone execution records, and long workspace detail lists with the shared i18n Pagination.
- [x] Keep the shared Pagination business-agnostic while supporting a compact presentation that can omit range statistics in narrow cards.
- [x] Preserve intentional spacing between page navigation, page-size selection, and quick jump controls.
- [x] Give every newly created Run a persisted, user-readable processing title and provide meaningful historical fallbacks from its work item or dispatch request.
- [x] Keep file trees, member lists, and graph canvases within the available viewport instead of growing the page.
- [x] Verify every tab and nested view at desktop and narrow widths without clipped controls or unexplained empty space.

## Visual hierarchy and product copy

- [x] Format the project workspace, shared session drawer, and project UI source with Prettier inside Docker before further edits.
- [x] Replace the milestone Run dump with a compact result summary and a paginated execution Drawer.
- [x] Clamp long project goals, milestone titles, evidence, event summaries, commit messages, and list descriptions; expose the complete value through an intentional preview rather than layout-breaking text.
- [x] Stabilize the cockpit objective summary for long project goals: keep the summary to two lines, retain full hover text, and preserve the two-column metric layout at desktop and stacked layout at narrow widths.
- [x] Normalize every project segmented control to the standard control height and remove capability-filter overrides so view, domain, workspace, member, and capability switches do not jump or change height between tabs.
- [x] Remove internal implementation language such as durable/runtime/atomic/idempotent/human-only policy explanations from the rendered UI.
- [x] Keep implementation decisions in code comments and architecture documents, not customer-facing labels or helper text.
- [x] Rework A2A, dependency, and snapshot graphs with readable professional layouts and Bezier connections.
- [x] Make all graph nodes that represent a real Run or conversation open the exact corresponding session.

## Content rendering and internationalization

- [x] Rename the customer-facing project role from “Leader” to “负责人” in Chinese and “Project owner” in English while preserving stable API and database field names.
- [x] Remove the internal “standard Web Chat connected” status and equivalent implementation-oriented hints from the session UI.
- [x] Move all new session, project-graph, and project-role copy into aligned Chinese and English translation catalogs.
- [x] Add a prebuild contract that rejects obsolete visible project terminology and verifies locale key parity.
- [x] Render work-item execution and event content with the standard Markdown renderer.
- [x] Collapse long event content by default and provide an explicit full Markdown preview.
- [x] Render evidence and approval content with Markdown; provide full Markdown preview on hover/focus/click without clipping.
- [x] Use Monaco for both editable Markdown source and the read-only Markdown view; persist view/source mode in the URL and avoid a second custom rendering stack.
- [x] Translate project Run, work-item, Git, group, and A2A event labels and descriptions through the shared i18n catalog.
- [x] Show the translated event label only; do not repeat the internal event code when a translation exists.
- [x] Make milestone file/diff evidence inspectable through the existing project file/diff viewer.
- [x] Derive each work item's file list only from explicitly related commits and their real Git diff; never treat requested metadata paths as proof of a change.
- [x] Keep empty milestone commits visible for traceability while reporting zero changed files.

## Exact session traceability

- [x] Every Run node and Run row must resolve to its exact visible session.
- [x] Persist a message anchor for each Run input/output/event.
- [x] When multiple Runs share one session, opening a Run scrolls to the corresponding turn anchor rather than only opening the session.
- [x] Keep the anchored turn visibly highlighted long enough to identify it, and respect reduced-motion preferences.
- [x] Automatically scroll an embedded project group chat to the latest message after the initial history load or project switch, without pulling users away after they deliberately browse older history.
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
- [x] Provision the standard `aio-sandbox` service in the 3011 Docker project so role-specific Agent tools resolve through the same Compose network.
- [x] Load a fresh project turn through its exact history anchor instead of requiring that anchor to remain the newest row; concurrent later inputs must not invalidate an already claimed turn or leak into its prompt.
- [x] Prevent durable project outbox starvation when more than 50 older delivered Runs remain visible to the dispatcher; filtering and pagination must happen without skipping newer undispatched work.
- [x] Add project file activity values through a safe enum migration and verify sent/received file events are durable and translated.
- [x] Resolve duplicate A2A file-session selection by consuming and validating the native transport's exact project Run, A2A Session, Subagent Run, and Subagent Session receipt; malformed or cross-scope receipts now fail durably instead of silently selecting the latest same-pair Session, while legacy plain-text transports retain their scoped compatibility lookup.
- Historical customer-support/compliance/operations/data/procurement replay is superseded by the clean final cross-domain project; the dispatcher and exact-anchor defects remain covered by Docker regressions.

## Domestic container image policy

- [x] Replace Docker Hub short names in backend/frontend Dockerfiles and all Compose stacks with explicit domestic mirror references.
- [x] Use the same configurable `CLAWITH_IMAGE_MIRROR` contract for local, single-instance, and multi-instance deployments; keep the existing domestic Volcengine sandbox image.
- [x] Default Helm-managed PostgreSQL and Redis images to the domestic mirror registry.
- [x] Route Drone runner images and current/legacy backend/frontend build stages through domestic registries without passing proxy variables to image-build steps.
- [x] Route on-demand Python, Bash, and Node sandbox images through the same domestic registry and reject runtime pulls whenever the Docker daemon exposes an HTTP/HTTPS proxy.
- [x] Add a pre-pull policy check that rejects unapproved image registries and refuses to proceed while the Docker daemon HTTP/HTTPS proxy is enabled.
- Docker Desktop `No proxy` remains an operator precondition enforced by the pre-pull policy check, not an unfinished product-development task.

## Platform concurrency and capacity

The application-level target is concurrent outstanding Turns, not database
connections or simultaneous upstream model requests. Admission, session
serialization, provider throttling, and short database transactions remain
independent controls so one slow dependency cannot consume every resource.

- [x] Admit at least 500 concurrent interactive Turns across at least 300 ordinary Web Chat or channel sessions on one application instance.
- [x] Reserve independent capacity for project/Subagent, scheduled, and background workloads so ordinary conversations cannot consume their full operating budget and background spikes cannot starve interactive traffic.
- [x] Use one business-agnostic admission component for Web Chat, external channels, project/Subagent Turns, schedules, triggers, and background task execution.
- [x] Preserve strict same-session serialization while allowing unrelated sessions to execute concurrently.
- [x] Keep capacity and provider queue waits outside database transactions; keep the database pool at `20 + 10` instead of mapping one connection to every outstanding Turn.
- [x] Apply the same provider-scoped concurrency gate to streaming Web Chat and non-streaming project/background completion calls; provider queue time must not consume the request timeout.
- [x] Return a retryable overload outcome after bounded admission wait instead of starting unbounded work or marking a durable scheduled/project Turn terminally failed.
- [x] Export low-cardinality active, waiting, limit, admitted, completed, rejected, and high-watermark Prometheus metrics for global and workload-category capacity; tenant identifiers must not become metric labels.
- [x] Verify 700 simultaneous Docker/PostgreSQL-backed Turn boundaries: 500 interactive Turns across 300 sessions plus 100 project, 50 scheduled, and 50 background Turns; include short reads before and after provider wait, same-session maximum concurrency of one, explicit overflow rejection, and a database-pool high watermark no greater than 30.
- [x] Run a sustained mixed workload against the rebuilt 3011 stack with ordinary chat, project A2A/Subagents, scheduled triggers, and background tasks active together; record latency percentiles, queue depth, error rate, and recovery after the spike.
- [x] Replace the PostgreSQL advisory session lock with one shared Redis owner-token lease with bounded acquisition, automatic renewal, atomic release, cancellation safety, and retryable failure semantics; no database connection remains checked out for the duration of model execution.

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
- [x] Screenshot every workspace tab in light and dark themes after the final 3011 rebuild.
- [x] Re-verify URL persistence, exact anchored sessions, Markdown expansion, i18n, diff viewing, snapshot editing, and model selection in the browser after the final 3011 rebuild.
- [x] Rebuild and recreate the latest backend/frontend images on port 3011 after the final visual and copy pass.
- [x] Capture and inspect a fresh screenshot for every top-level tab after the final rebuild; record and fix every overflow, blank, stale, or untranslated state before closure.
- [x] Merge the latest main branch and join the project and chat migration branches into one Alembic head.

## Professional role collaboration remediation

The acceptance condition is not “a message exists”. Each project participant
must contribute a role-specific professional judgment that changes a decision,
artifact, review outcome, or next action.

- [x] Inject the immutable project name, goal, success criteria, member name, member role, Soul, and Core Memory into every group, direct A2A, and project Subagent turn.
- [x] Preserve source member identity and role across coalesced responsible-person batches and preserve evidence attribution, dissent, rationale, owners, and next actions during standard context compaction.
- [x] Replace blank project-Agent identity files with project- and role-specific default Soul and Core Memory; copied Agents retain their authored identity and only missing files receive defaults.
- [x] Prohibit project-runtime A2A from being used as a passive notification channel. A project Agent may consult or delegate one actionable professional request, but may not wake another role for FYI, progress announcements, acknowledgement, or waiting.
- [x] Require every delegated project A2A action to have a durable title and a related work item, and require the message to state evidence/context, the professional decision or task, and the expected output.
- [x] Tell the responsible person not to pre-wake downstream roles before their dependencies are ready; passive progress belongs in work-item/project records and must not consume an Agent Turn.
- [x] Add cross-domain semantic gates that reject trivial, duplicate, role-agnostic, and mechanically varied status acknowledgements even when the Run technically succeeds.
- [x] Make project group chat the sole writable planning transport; preserve the legacy responsible-person planning session as server-enforced read-only history.
- [x] Prevent a second kickoff while a planning Run is queued/running or while the latest human planning message still lacks a terminal responsible-person response.
- [x] Remove generic collaboration-send tools from project child runtimes so project A2A cannot bypass project lineage, target validation, and wake-storm controls.
- [x] Detect customer-visible internal narration such as “now writing” or “let me update” and allow at most one no-tool, no-wake semantic self-correction.
- [x] Freeze the bounded original Human request and work-item acceptance, dependency, and evidence context into each responsible-person batch Run; retries reuse the persisted snapshot.
- [x] Freeze the same bounded work-item context at the shared REST/runtime A2A enqueue boundary so later edits cannot rewrite an already delegated professional task.
- [x] Cover development, product/UX, marketing, data, HR, operations, customer support, procurement, compliance, and release/operations with at least five distinct professional roles and real domain-specific deliverables per project.
- [x] Rebuild 3011 with this contract and run a fresh real project; inspect the group conversation and every direct A2A thread for evidence, professional challenge, trade-offs, decisions, and concrete handoffs with no status-only wakeups.

## Acceptance evidence

- Software-development acceptance: `PulseOps` project `04387e1c-a2d2-4523-9480-4001b6afc54b` completed with 5/5 work items, 12/12 successful Runs, 147 events, 3 milestones, and Git HEAD `54730054fa1cf684bf9c39989ac0a5cc301e6ecb`. Its historical null Run/work-item and incomplete milestone-link rows remain historical evidence rather than an open release item: current creation paths persist exact lineage, `b0c2e14b` automatically resolves successful Runs for selected work items, and RC3 verifies complete links from a clean project.
- Cross-domain batch `真实业务验收·20260821` uses public APIs and real tenant Agents only. Project IDs: marketing `fe7c78a6-f49e-48d9-b34e-948961b41645`, data `37e7eaf9-46b8-4bdc-9b2c-d50a72424d85`, HR `234e0e29-d829-4d50-97da-9a4eb7024bd5`, operations `ccee7cae-39c0-4615-b050-2704636149be`, support `74a86053-9b98-4587-8c7c-58f6c2cea993`, procurement `07291247-4986-4d48-b006-0f64d650e9c2`, and compliance `64db50c9-aa45-47f2-bca9-afeedd88e495`.
- Cross-domain runtime findings are acceptance evidence, not ignorable noise: same-session concurrent replies exposed exact-anchor prefix validation failures; more than 50 historical delivered Runs exposed durable outbox starvation; the 3011 Compose project initially lacked its declared `aio-sandbox`; project file activity exposed enum drift; and duplicate A2A file-session lookup exposed an incomplete identity constraint. Each finding remains open until its source fix, Docker regression test, recovered Run, and exact-session browser trace all pass.
- Strict cross-domain rerun (2026-08-24): the historical batch exposed missing lineage, failed Runs, weak role evidence, status-only collaboration, weak topology, and incomplete milestone links. Those findings were subsequently repaired and rerun from a clean project. RC3 project `fb454fa3-ccb1-4f71-b587-a29ed200c3b6` is the authoritative closeout evidence and passes the strict gate.
- Real project: `cda3cb3d-142c-48e3-a9a6-b7ffcedd2e86`; status `completed`; 8/8 work items done; 49 durable ProjectRuns; 6 distinct project Agents; 9 Git deliverables; one typed M1 release milestone; no queued or running residue.
- Session integrity: every ProjectRun resolves to an existing project-scoped session and immutable member snapshot; Run links use exact session and message anchors; group, direct A2A, work-item, milestone, Git, snapshot, and audit entry points share the same routing contract.
- Context integrity: project execution uses the standard caller, context recovery, compaction, confirmation, durable inbox, and restart recovery path. The long acceptance project remained below its 85% compaction threshold; zero compactions was therefore expected, not a bypass.
- Timeout remediation: all observed 180-second failures were provider-stream timeouts after tool completion, amplified by overlapping model requests. Provider-scoped concurrency, queue-time exclusion, zero-output TTFT retry, activity renewal, and an absolute hard cap are covered by tests.
- Model verification: `qwen3.5-plus`, `qwen3.6-plus`, and `qwen3.7-plus` were cloned idempotently from the local tenant model without exposing credentials, shown in the tenant selector, and answered live health requests successfully.
- Automated verification: 151 focused backend tests passed; the complete frontend prebuild suite, TypeScript compilation, and production Vite build passed; Docker backend and frontend were rebuilt and reported healthy.
- Browser verification: all 14 workspace tabs loaded without loading/error residue; URL refresh restored audit filters and work-item detail tabs; Monaco displayed a real Git diff; the model selector showed all four Qwen variants; light and dark screenshots were captured in `artifacts/qa-2026-08-21/`.
- Capacity verification: Docker acceptance `backend/scripts/acceptance/run_workload_capacity_probe.py` completed 700 simultaneous Turns against PostgreSQL in 0.951 seconds: 500 interactive Turns across 300 sessions plus the full reserved lanes of 100 project, 50 scheduled, and 50 background Turns. It observed same-session maximum concurrency `1`, admission high watermark `700`, one intentional overload rejection, and database-pool checked-out high watermark `30/30`. Machine-readable evidence is `artifacts/capacity-2026-08-22/700-mixed-turn-postgres-probe.json`.
- Sustained-capacity verification: Docker acceptance `backend/scripts/acceptance/run_sustained_workload_probe.py` completed five consecutive 700-Turn waves (3,500 total) across the same 300 interactive sessions and reserved project, scheduled, and background lanes. Turn latency was p50 `517.78 ms`, p95 `718.98 ms`, and p99 `819.27 ms`; queue high watermark was `25`; all `125` intentional overflow attempts received retryable overload outcomes; error count was zero; database checked-out high watermark remained `30/30`; and the limiter returned to zero active and waiting work in `0.03 ms`. The live backend health endpoint returned HTTP 200 before and after the spike. Machine-readable evidence is `artifacts/capacity-2026-08-22/3500-sustained-mixed-turn-probe.json`.

## Project-dedicated Agent API contract

> Implementation note (2026-08-22): this section records the final minimal
> contract. A project Agent is a regular Agent whose mutable assets belong to
> the project Git repository. There is no separate profile/version subsystem,
> no `.clawith` directory, and no project-specific runtime implementation.

## Project-wide runtime switch (2026-08-24)

`Project.status` is the single authoritative runtime switch. A running project
can transition to `paused`, and a paused project can transition back to
`running`; no second Boolean, per-member shadow state, or transport-specific
pause flag is introduced. The transition is an owner-authorized control-plane
command and emits a durable project event. Historical messages, Runs, events,
sessions, and Git evidence remain unchanged.

The execution boundary must enforce the same state independently of the UI:
new project group-message wakes, explicit Project Runs, REST/runtime A2A wakes,
and project-Agent schedules/triggers are rejected or deferred while paused.
The durable ProjectRun dispatcher joins the Project row and only claims work
for running projects, then rechecks the state under lock immediately before it
creates/appends a Subagent turn. Queued rows therefore remain traceable and are
not silently consumed; restoring the project makes them eligible again.
Already executing model/tool turns are not force-killed, because interruption
could corrupt externally visible side effects. Pausing prevents every later
queue claim and continuation from that project.

- [x] Add one owner-only pause/resume endpoint over `Project.status`, validate
  legal transitions, and persist typed pause/resume audit events.
- [x] Centralize project-running checks and apply them to Project Run, group
  message, REST A2A, runtime A2A, durable dispatch, schedules, and triggers.
- [x] Add the project header pause/resume action with the standard Button,
  confirmation Dialog, loading/error feedback, and i18n copy.
- [x] Refresh all project data after a transition and keep existing history and
  traceability views available while the project is paused.
- [x] Format and run focused backend/frontend Docker validation without
  changing Plaza or introducing business state into generic UI components.

## Standard Digital Employee project-management tool group (2026-08-24)

This iteration gives a standard Digital Employee an explicitly enabled,
Human-delegated project-management capability. It does not expose the
contextual `project_runtime_tools` used by project child Runs to an ordinary
chat session. The new group is a small adapter over the existing project
services and authorization checks, backed by database `Tool` definitions and
explicit `AgentTool` assignments like every other ordinary LLM tool.

### Enablement and product entry

- [x] Seed one stable project-management group for standard Digital Employees,
  with every member disabled by default. `is_default` is only a creation
  template; runtime availability still requires explicit
  `AgentTool(enabled=True)` rows.
- [x] Expose one group switch in the standard Digital Employee tool manager.
  Enabling or disabling the group updates every required member atomically and
  rejects an incomplete or mixed request; it must not depend on a translated
  category label or a frontend-maintained tool-name list.
- [x] Keep the project Capability Center separate. Its project-member tool
  controls continue to resolve role, project policy, member snapshot, and
  lifecycle for a project child Run; they never mutate the source standard
  Digital Employee's ordinary `AgentTool` assignments.
- [x] Show the scope before confirmation: “Only available when this Digital
  Employee is assisting a real person in an authorized project. Enabling it
  does not add the Digital Employee to a project or grant project access.”
- [x] Return the canonical group state (`disabled`, `partial`, or `enabled`),
  member count, and non-secret availability reason from the backend so the UI
  never reconstructs effective permission from seeded metadata.

### Human-turn and authorization boundary

- [x] Admit the group only for a genuine Human Web/IM turn whose session and
  inbound message resolve to the authenticated or provider-mapped person.
  A populated creator, owner, or execution `user_id` on an A2A, trigger,
  schedule, task, Subagent, recovery, or other background turn is not Human
  authority and must not unlock these tools.
- [x] Resolve project visibility and each requested action from that real
  conversation person's current project ACL on every call. The Digital
  Employee's creator, manager, tenant membership, project membership, or
  prior successful call cannot substitute for the person's current access.
- [x] Require an explicit complete `project_id` for every operation, validate
  tenant and project ownership without cross-tenant disclosure, and reject
  ambiguous name-based project selection. Object identifiers must be verified
  inside the same authorized project.
- [x] Preserve backend enforcement after the UI hides an unavailable action:
  view authority permits only reads; project edit/owner authority is required
  by the existing domain command; owner-only access, sharing, runtime switch,
  repository credentials, Agent lifecycle, and template publication remain
  owner-only.
- [x] Recheck authority immediately before a write and after any confirmation
  resume. A delayed tool call must not reuse an ACL decision captured before a
  membership, share, project-status, or conversation-identity change.

### Bounded read tools and atomic write tools

- [x] Keep read operations explicit and bounded: list the person's accessible
  projects, read one project summary, and page one project's work items,
  members/capabilities, Runs, events, milestones, or repository paths through
  the existing authoritative services. No read tool returns an unbounded
  project dump or silently follows a “latest project” fallback.
- [x] Require server-side cursor/page size limits, stable ordering, and
  response metadata for every collection. Tool descriptions must tell the
  model to continue with the returned cursor rather than requesting or
  retaining an entire history in context.
- [x] Model every write as one small domain command with one auditable result,
  such as creating one work item, updating allowed fields on one work item, or
  adding one Human-authored project-group message. Do not add a generic
  project patch tool, multi-project batch mutation, arbitrary settings JSON,
  raw SQL/file access, or a tool that combines read, inference, and several
  writes.
- [x] Reuse the existing project services, validation, status transitions,
  idempotency/operation keys, Git commit boundary, pause checks, and durable
  outbox where applicable. Tool adapters must not call a private alternate
  mutation path or implement a second project lifecycle.
- [x] Keep high-risk writes out of the standard Digital Employee project tool
  group. Sharing, credentials, repository replacement, restore, deletion, and
  publication remain owner-facing controls; any future tool exposure must use
  the shared Human confirmation flow for the displayed atomic operation only.

### Audit, pagination, and context isolation

- [x] Persist the real Human actor, assisting Digital Employee, source session,
  inbound Human message anchor, tool-call anchor, project, target object, and
  outcome in the existing audit trails. Successful mutations append a project
  event; every invocation appends a bounded platform activity record. Rejected
  and failed invocations remain truthful platform outcomes and are never
  rewritten as assistant success.
- [x] Keep project events, Chat messages, Project Runs, Git objects, and
  platform audit records as separate evidence types. Cross-links may share a
  trace presentation, but a tool call must not synthesize one record type from
  another or use a Project Run ID as a session/message identifier.
- [x] Bound tool-result text and nested fields before adding them to LLM
  context. Include only the selected project's necessary goal, status, object
  summary, ACL-derived available actions, and pagination state; exclude other
  projects, full conversations, full audit history, credentials, private MCP
  configuration, rendered environment/header values, and repository secrets.
- [x] Do not write project data into the standard Digital Employee's durable
  Soul, core memory, or workspace as an implicit cache. Subsequent turns must
  re-read authoritative project state and ACL instead of trusting remembered
  access or stale collection results.
- [x] Verify observable behavior for disabled/partial/enabled groups, Web and
  mapped IM Human turns, non-Human entry points carrying a `user_id`,
  owner/editor/viewer/removed access, confirmation resume after ACL change,
  pagination beyond the first page, paused projects, cross-project object IDs,
  and complete audit/session/message anchors.

## Global Plaza retirement (authorized 2026-08-24, reconfirmed 2026-08-27)

Plaza is disabled as one authorized platform invariant rather than a
navigation-only change. Historical database rows remain available for audit
and rollback, but there is no user route, public API router, heartbeat
instruction, LLM tool exposure, or direct runtime dispatch that can create,
read, or comment on Plaza content.

- [x] Redirect the retired frontend route to the supported discovery page and
  remove onboarding, layout, access-scope, and heartbeat copy that advertises
  Plaza.
- [x] Stop registering the Plaza API router so direct HTTP calls are not a
  supported product surface.
- [x] Force all Plaza builtin tools globally disabled during seed/sync and
  exclude them from every digital employee LLM tool roster, including existing
  assignments restored from an old database.
- [x] Reject direct Plaza tool dispatch at the shared runtime boundary so a
  stale queued call or historical tool assignment cannot bypass the switch.
- [x] Preserve this authorized decision during the full branch-to-main audit;
  do not reinterpret an explicit product removal as an unintended regression.

## Workspace information-architecture consolidation (2026-08-24)

The independent UX review in
`docs/plans/2026-08-24-project-workspace-ia-second-opinion.md` is the source of
truth for this pass. The implementation must preserve every existing deep link
while removing duplicate first-level destinations.

- [x] Replace the 13-item sidebar with six task domains: Overview, Work,
  Collaboration, Delivery, Team, and Activity. Project Settings remains the
  single header action and must not be duplicated in the sidebar.
- [x] Keep the existing feature routes as URL-compatible second-level views:
  Work (list/board/dependency/detail), Collaboration (chat/A2A), Delivery
  (workspace/milestones/version history), Team (members/capabilities/access
  matrix), and Activity (Runs/Events).
- [x] Persist both the selected task domain and second-level view in the URL;
  refreshing or opening a deep link must restore the same state.
- [x] Make Overview a bounded summary only: no full duplicate browsers and no
  pagination; each preview is capped and deep-links to its canonical domain.
- [x] Remove full duplicate Run/event/file/Commit lists from object pages;
  object pages show a bounded contextual summary and link to the canonical
  Activity or Delivery view.
- [x] Recheck the consolidated navigation, nested views, narrow layout, theme
  tokens, copy and exact session links in Docker on port 3011 before marking
  this section complete.

Evidence (2026-08-24): `WORKSPACE_DOMAINS` now defines exactly six first-level
domains while legacy tab values remain accepted by `projectWorkspaceRouting`.
Overview caps work and risk previews at five records. Work-item details cap
contextual Runs, events, sessions, files, and commits at five and link to the
canonical Activity or Delivery view. Port-3011 browser verification restored
`?tab=work&workView=list` after reload with the list view still selected. A
second port-3011 pass on 2026-08-24 confirmed the six-domain navigation,
`?tab=group` restoration, standard Web Chat tool/thinking rendering, a
1024-pixel icon-only navigation rail, and zero document-level horizontal
overflow. Markdown view/source now share the Monaco editor and URL state.
The 2026-08-25 pass rebuilt both images, reused the existing collapsed AppShell
at 390 pixels, confirmed a 322-pixel project content area with no horizontal
overflow, and normalized Collaboration and Delivery content to one shared
gutter contract. Exact-session and copy/i18n contracts passed in the Docker
frontend prebuild; full real-project acceptance remains intentionally deferred
by the project owner.

### Existing boundaries and gaps

- The current project-member endpoints only attach a tenant Agent through `ProjectMemberSnapshot.agent_id` and freeze selected configuration into `config_snapshot`; they do not create an independently editable project-local Agent.
- Member creation, removal, restoration, and snapshot editing currently use the generic project `edit` grant. That permission is intentionally broader than the proposed project-Agent administration permission and must not be reused as an implicit owner check.
- Global Agent creation and editing are creator/admin operations. They do not express project ownership, project-local lifecycle, copy provenance, or promotion semantics.
- Project-template `definition` is currently an open dictionary. Publishing has no recursive service-side guard that rejects credentials, conversation identity, runtime state, or usage/accounting fields.

### Minimal data model and endpoint surface

A project Agent is a standard `Agent`, not a separate ProjectAgent/Profile/Version resource. The existing Agent record gains only the project-scoping and provenance fields required by the lifecycle:

- `scope`: `standard` or `project`;
- `project_id`: required only when `scope=project`;
- `source_agent_id`: optional immutable provenance for a copied Agent;
- `agent_dir`: project-relative asset root.

Each project Agent owns one fixed project asset tree. No generated version directory or second profile store is introduced:

```text
.agents/<agent_id>/
├── soul.md
├── memory.md
└── workspace/
```

The standard Agent APIs remain the source of read/runtime behavior. Project-specific mutations are intentionally small:

| Operation                 | Endpoint                                                   | Required authority         | Atomic result                                                                                                                                                           |
| ------------------------- | ---------------------------------------------------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Create or copy            | `POST /projects/{project_id}/agents`                       | Project responsible person | Creates a standard Agent row with `scope=project`; optional `source_agent_id` copies the approved fields/assets; active membership and audit event are created together |
| Modify                    | `PATCH /projects/{project_id}/agents/{agent_id}`           | Project responsible person | Updates the project Agent and its project assets without mutating the source Agent                                                                                      |
| Deactivate                | `POST /projects/{project_id}/agents/{agent_id}/deactivate` | Project responsible person | Stops new work while preserving all historical project references                                                                                                       |
| Restore                   | `POST /projects/{project_id}/agents/{agent_id}/restore`    | Project responsible person | Re-enables the same project Agent and its preserved assets                                                                                                              |
| Promote to standard Agent | `POST /projects/{project_id}/agents/{agent_id}/promote`    | Project responsible person | Creates a new `scope=standard` Agent copy whose `source_agent_id` points to the project Agent; the project Agent remains in place                                       |

Create/copy/promote use the existing project transaction and recoverable Git
workspace boundary. Cross-project and cross-tenant source IDs remain
indistinguishable from missing resources (`404`). The interface intentionally
does not add a second operation protocol or business-specific idempotency layer.

### Authorization and lifecycle invariants

- “Project responsible person” means the human project owner. A shared viewer/editor and an ordinary participating Agent cannot create, copy, edit, deactivate, restore, promote, or publish project Agents.
- Project ownership is checked independently of the generic project `edit` grant. Tenant and project IDs are validated on every referenced Agent, model, capability, member, and template.
- A deactivated project Agent remains visible in member, Run, session, A2A, Git, milestone, and audit history, but cannot be selected as an assignee or target. Mention, A2A, group-wake, manual Run, scheduled Run, trigger, and background entry points return the same stable `409 project_agent_inactive` failure before enqueueing work.
- Copying never aliases mutable Agent configuration, `soul.md`, `memory.md`, `workspace/`, capability assignments, or membership state. Later source changes do not mutate the project copy.
- Promotion is copy-only. It creates a new Agent ID with `scope=standard` and `source_agent_id` pointing to the original project Agent; every existing project reference continues to resolve to the original project Agent.
- Destructive or high-risk capability policy cannot be weakened beyond tenant policy during create, copy, patch, or promote.

### Copy and template data boundary

### Project template final-asset contract

Publication and restoration form one final-state portability contract. A
template is not complete merely because its metadata or project-agent identity
files can be saved: the packaged final tree, Digital Employee assets, safe
configuration, market manifest, and `from-template` restore path must agree.

#### Publication checklist

- [x] Resolve one immutable source Git HEAD and package the complete checked-out
  final project tree at that HEAD, including deliverables that are not listed
  in a milestone. Store integrity metadata for the package; do not build the
  template from event path hints, browser state, or a later mutable worktree.
- [x] Export every active or historical project-owned Digital Employee that is
  part of the reusable final state with a fresh-identity manifest containing
  display name, project role/leader designation, sanitized `soul.md`, sanitized
  core `memory.md`, and every safe UTF-8 workspace file under
  `.agents/<agent_id>/workspace/`.
- [x] Include safe model references, bounded runtime settings, autonomy/safety
  policy constrained by current tenant policy, contextual project-tool
  enablement/deny state, and safe capability references with their enabled
  state. Do not serialize live ORM/database identity as the portable key.
- [x] Include project goal, success criteria, non-secret project settings, and
  the safe final-state relationships needed to restore roles and capability
  assignments. Sharing grants, owner identity, and source-project storage
  locations are not portable configuration.
- [x] Recursively sanitize the entire manifest and every packaged file before
  persistence. Reject unsafe paths, symlinks, unsupported/binary files outside
  the approved asset policy, oversize payloads, duplicate paths, and any
  structural field outside the explicit allowlist with a stable `422` error.
- [x] Exclude development and execution history: conversations, messages,
  compactions, tool calls/results, active turns, Project/Subagent Runs, A2A and
  project events, audit logs, schedules, triggers, callbacks, approvals,
  queues/leases, usage/accounting, sandbox/container state, and Git history.
  The final checked-out artifacts remain complete even though their history is
  intentionally absent.
- [x] Exclude every private MCP and secret-bearing value: MCP credentials and
  bindings, rendered headers/environment, private endpoints, API keys/tokens,
  channel credentials, repository remotes/credentials, and tenant/user secret
  configuration. Only a safe shared capability reference and non-secret policy
  may cross the boundary; an unresolved/private capability is reported as
  excluded rather than copied or silently substituted.
- [x] Persist a viewer-safe publication manifest with final-asset count/size,
  project-owned Digital Employee names/roles and safe asset counts, safe
  Skill/shared-MCP reference counts, and excluded-category counts. Never expose
  internal UUIDs, private MCP names/endpoints, secret presence, or repository
  paths in the template-market payload.
- [x] Make the publish dialog show the server-produced manifest before the
  final command: final project assets, included project-owned Digital
  Employees, safe shared capabilities, and an explicit “private MCP and
  credentials are excluded and must be configured after creation” notice.
  The UI must not infer this summary from the current file tree.
- [x] Commit the sanitized package and template definition atomically, or leave
  neither visible. Publication retry must be idempotent and must not mix a
  definition from one source HEAD with assets from another.

#### Restore and market-use checklist

- [x] Return the same viewer-safe manifest from template list/detail APIs and
  render it in the market card/detail: final assets, project-owned Digital
  Employees, safe Skill/shared-MCP references, and excluded/private setup
  requirements. Use semantic counts and names, never internal IDs or paths.
- [x] Make “Use template” submit through the authoritative
  `POST /projects/from-template` contract. Passing only `template_id` to the
  ordinary project-create endpoint or client-side matching capability names is
  not template restoration.
- [x] Create a fresh Project identity and independent managed Git repository,
  unpack the sanitized final tree, and create one new baseline commit without
  importing the source Git history, remotes, credentials, or mutable storage.
- [x] Create fresh database identities for every packaged project-owned Digital
  Employee, remap every `.agents/<old_id>/` path and all internal manifest
  references to the new IDs, and restore Soul, core memory, safe workspace
  files, role/leader state, model/runtime policy, and safe capability state.
- [x] Resolve model and shared capability references only within the target
  tenant's visible catalog and current policy. Missing, private, disabled, or
  policy-incompatible references become explicit “configuration required”
  results; they are never matched by display name, inherited from the publisher,
  or silently replaced with a more privileged capability.
- [x] Show the user a pre-create “will restore” summary and a post-create setup
  summary. Private MCPs and credentials remain absent; the new project may only
  use them after the new owner explicitly connects an authorized target-tenant
  configuration.
- [x] Make restoration transactional across database records, filesystem
  package extraction, identity remapping, repository initialization, member
  snapshots, capability bindings, and initial project sessions. On failure,
  compensate newly created storage and expose no partially usable project.
- [x] Verify final-state equivalence through observable APIs and Git content:
  root deliverables, Digital Employee assets, roles, safe model/runtime policy,
  capability enablement, and fresh identities match the manifest; excluded
  history, private MCP data, credentials, source IDs, remotes, and Git history
  are absent.

Allowed copy data is an explicit allowlist: display identity, role description, authored persona/prompt fields, safe model references, safe runtime limits, capability references with enabled state, autonomy policy constrained by tenant policy, and non-secret project-role metadata. It may also copy the source `soul.md`, a sanitized core subset of `memory.md`, and safe files under `workspace/`.

A project template may include the same sanitized `soul.md`, core `memory.md`, and safe `workspace/` files so a reusable project role retains useful context. Publication validates paths, file types, size limits, tenant policy, and content before storing the template payload.

Evidence (2026-08-24): the owner-only
`GET /projects/{project_id}/template-manifest` endpoint and the publication
command call the same immutable-HEAD manifest builder. The publish dialog
blocks its final command until that server response is available, shows exact
final-file, Digital Employee, Skill and shared-MCP counts and safe names, and
states that private MCP configuration and credentials are excluded. The
viewer-safe response contains no file content, source identity, private MCP
name/endpoint or credential-bearing value. Backend container compilation,
frontend container TypeScript compilation and `git diff --check` completed
successfully after this contract was connected.

Historical-member evidence (2026-08-24): template export now selects the full
project-owned Digital Employee roster rather than filtering out inactive
member snapshots. The portable manifest preserves each member's enabled state;
restoration recreates inactive members as stopped, non-responsible historical
members while keeping their sanitized Soul, core memory and workspace assets.
Capability remapping uses the same full ordered roster. The Docker-backed
template asset and service suites completed with `10 passed`.

Restore-closure evidence (2026-08-24): publication now locks the source
project, captures one immutable HEAD, and persists the sanitized package,
definition and private retry fingerprint in one database row. An identical
retry returns the completed publication rather than creating a duplicate.
The create flow presents the authoritative pre-create manifest and a standard
post-create summary for restored project files, project-owned Digital
Employees, Skills, shared connections and tools. All restore stages remain in
the request transaction, and any filesystem, identity, binding, session or
summary failure removes the exact newly-created managed project directory so
no partially usable project remains. The observable round trip uses fresh
project, repository and Digital Employee identities while preserving final
files, roles, portable runtime/member policy and safe capability state; source
IDs, credentials, remotes and source Git history stay absent.

The following data never crosses a copy, promotion, or template-publication boundary:

- API keys, credential bindings or rendered MCP headers/environment values;
- channel accounts/tokens, external conversation IDs, participant IDs, and access grants;
- Chat sessions, messages, tool-call history, compactions, active-turn state, Subagent runs, Project runs, triggers, schedules, callbacks, or queued work;
- daily notes, focus/task history, non-core memory, workspace execution residue, temporary files, and repository credentials;
- container/sandbox identifiers, leases, heartbeats, online state, failure state, usage counters, quotas consumed, approval instances, and audit records.

Publishing recursively sanitizes the complete nested definition with the shared
template asset sanitizer before persistence. Credentials, runtime identity,
conversation state, and unsafe paths are removed; invalid structural shapes
return `422`. Template reads and project creation responses never expose secret
presence. Instantiation always creates fresh Agent IDs and fresh project-local
asset trees.

### Contract-test rollout

- [x] Project owner can create/copy, patch, deactivate/restore, and promote a project-scoped standard Agent.
- [x] A project editor/viewer and a non-owner participating Agent cannot administer project Agents or rewrite `soul.md` / `memory.md` through the generic file API.
- [x] Project Agents cannot be attached to another project; standard Agents remain reusable across projects.
- [x] Runtime storage resolves to `.agents/<agent_id>/` inside the owning project and rejects execution outside that project.
- [x] Agent writes to `soul.md` and `memory.md` are blocked in runtime storage, tools, and collaboration paths; owner updates remain available through the project Agent API.
- [x] Deactivation preserves history and blocks new execution; restoration reuses the same project identity and assets.
- [x] Promotion creates a distinct standard Agent and leaves project references unchanged.
- [x] A published template persists only sanitized `soul.md`, core `memory.md`, and safe `workspace/` files; template instantiation creates fresh project Agent identities and the project group/planning sessions.
- [x] Lifecycle, cross-project isolation, template persistence, runtime storage, and write protection are covered by Docker-backed API and service regression tests.
- [x] Expose the same lifecycle in the project workspace: create blank, copy from an existing Agent, edit Soul/Core Memory, deactivate, restore, promote, and open the exact `.agents/<agent_id>/` asset directory.
- [x] Rename the file module to “工作区 / Workspace” and present `.agents/` as a normal project-owned asset tree instead of introducing a second Agent file manager.
- [x] Present one workspace tree with two parallel user-facing roots: “项目 / Project” for deliverables and “数字员工 / Digital Employees” for `.agents/<id>/` assets; keep the internal storage path out of the navigation label.
- [x] Make the workspace tree/editor divider resizable with pointer and keyboard controls, bounded widths, responsive fallback, and persisted width.
- [x] Coalesce pointer-driven divider updates to one render per animation frame, remove the resize-observer feedback loop, delay local preference persistence until resizing settles, and keep the active divider visual state stable so resizing remains free of high-frequency jitter.
- [x] Add compact hover/focus actions to every directory for recursive expand/collapse and archive download; keep ordinary-file download as a single hover/focus action.
- [x] Keep HTML preview CSP headers bounded for deeply nested `.agents/<id>/workspace/` paths so the reverse proxy can render project-local HTML and relative assets without a 502 response.
- [x] Use one shared 12 px navigation typography token for the project tab rail and workspace tree; keep per-node download actions hidden until hover or keyboard focus.
- [x] Validate the `project_scoped_agents` migration delta on an isolated PostgreSQL database at revision `agent_file_activity_actions`; the expected four columns and final revision were verified after upgrade.
- [x] Re-run the focused Docker regression suite after the UI and Git changes: 75 tests passed; frontend production build and project prebuild contracts passed.

## Project API latency remediation

The project detail endpoint itself was already fast. The slow workspace and
cockpit responses came from one Git subprocess per file for metadata, preview,
and last-commit lookup. The repair stays inside the existing project Git
service: one `ls-tree`, one `git log --name-only`, and one bounded
`cat-file --batch` replace the N+1 loop. No cache, background index, duplicate
state store, or invalidation protocol was added.

- [x] Preserve the existing project API and response shape.
- [x] Bound file preview reads to 64 KiB per blob and 2 MiB per response.
- [x] Keep immutable Git as the only source of workspace file truth.
- [x] Verify warm 3011 latency on the real 77-file project: workspace files `7.214 s -> 0.150 s`; cockpit dashboard `13.601 s -> 0.227 s`; project detail remained below `0.010 s`.

## Product-first closeout (2026-08-24)

- [x] Reuse the dashboard project summary during the initial workspace load and remove the redundant project-detail request; keep silent refresh bounded to independently changing resources.
- [x] Move the project portfolio's user-visible status, filter, empty-state, action, time, and accessibility copy into the shared Chinese/English i18n catalogs.
- [x] Replace the unsupported `Intl.ListFormat` dependency in milestone summaries with locale-aware lightweight joining so the repository's existing TypeScript target remains valid without a new polyfill.
- [x] Build the product frontend once in Docker, deploy the generated assets to the existing 3011 container, reload nginx, and confirm the entry page returns HTTP 200.

## Final mainline, rollback, and fault-isolation closeout (2026-08-26)

- [x] Merge `yybpc/company/main@727c5f2b` through merge commit `5a664c72`; preserve mainline attachment/quoted-message behavior and the project conversation resume/anchor contract in the single shared timeline.
- [x] Merge `yybpc/company/main@26bcd4b5` through `c658ce8c`; the application candidate at that gate was `c94f8d52`. The later full-diff closeout merged `yybpc/company/main@d963d85c` through `e04de8dd`; the Plaza authorization correction froze application code at `d8d6263d`.
- [x] Build candidate frontend image `clawith-ai-project-frontend-check:merge-5a664c7`: full prebuild, TypeScript, and Vite production build passed, with 10,297 modules transformed and only the existing large-chunk warning.
- [x] Pass seven focused frontend behavior commands covering attachments, Web resume, H5 timeline, project routing, project i18n, Git diff, and project file workspace. Candidate nginx reached backend health `status=ok`, version `1.10.3`; project, team, Agent tool, and Skill routes loaded or redirected to login with zero console errors/warnings.
- [x] Add one observable PostgreSQL matrix for standard Digital Employee project tools. Together with the existing group-state behavior test it passed `8` tests and covers disabled/partial/enabled, Web/mapped IM Human identity, legal non-Human `subagent` rejection, owner/editor/viewer/removed ACL, ACL changes across confirmation resume, pagination, paused projects, cross-project IDs, and separate audit/session/message anchors.
- [x] Correct successful work-item creation audit linkage so the completion ProjectEvent references the new work item returned as `id`.
- [x] Keep project runtime checks behind an explicit project-Agent boundary. A failed project query or unavailable project service must not block standard-Agent conversation, scheduled task, manual task, trigger claim, or trigger invocation paths. Docker/PostgreSQL fault-isolation coverage passed `10/10`; related background/manual/scheduler regression passed `36/36`; Ruff, format, and Python compile checks passed.
- [x] Complete the compatibility-downgrade exercise against exact old backend `v1.10.3-bd5cb26` and close the known-ID/worker boundary that an Agent-list-only check missed. The candidate helper creates a dedicated non-owner legacy role, applies reversible restrictive RLS to every Agent foreign-key and `project_id` boundary, and restores each table's exact prior RLS state without changing business rows. Exact old API/worker roles stayed healthy with restart count zero; standard Agent CRUD and task/schedule/trigger APIs remained available; known project Agent detail, session, task, schedule, trigger, tool, permission, activity, approval, and gateway APIs returned not-found; global inbox/notification/tool/Skill/Plaza/page lists had no project marker; and a queued project Subagent, schedule, and trigger remained unclaimed after worker ticks. Restore was idempotent and the project Agent/member/session/Run/event/message ID snapshot retained checksum `fa0afa5b04f4838c480a24d54d19dce9` before and after restore.
- [x] Adopt the revised rollback criterion: compatibility downgrade must let the old service and all pre-existing capabilities run safely; rollback does not need to erase every additive project schema/history change. Point-in-time PostgreSQL/AgentData restoration remains the disaster-recovery path for corruption or a failed compatibility downgrade.
- [x] Complete milestone trace links in `b0c2e14b`: selecting work items automatically links their same-project successful Runs, explicit identifiers remain bounded, and failure recovery remains idempotent while incorporating later successful Runs.
- [x] Enforce `max_parallel_runs` as one atomic project gate across every ProjectRun type. Saturated work remains queued without a false running timestamp, capacity release permits the next atomic claim, and ordinary Subagents follow an isolated non-project claim path.
- [x] Run the post-merge Docker impact suite: `78 passed`. Restart the 3011 backend and verify health HTTP 200 with restart count 0.
- [x] Pass strict RC3 cross-domain acceptance with project `fb454fa3-ccb1-4f71-b587-a29ed200c3b6`: 4/4 work items, 32 Runs, five roles, 200 events, two milestones, no failed/active/unrecovered/unlinked records, and Git HEAD `5f46a1483080e32236343a302e560a533352174f`. Evidence checksum: `1cfbbd26974d3b6a819ce7f317e312a26e7180796e6143ca33ee70411e40a090`.
- [x] Exclude complete keyboard and screen-reader review by explicit user decision. Existing accessible controls remain, but this review is neither claimed complete nor counted as an open development item.

All product-development checklist items in this document are now implemented,
verified, superseded by the approved fresh-project acceptance scope, or
explicitly excluded. Complete keyboard and screen-reader review is waived by
the user and is not remediation debt. The application candidate is `GO for
production preparation`; this is not a production release or an authorization
to build/push production images, stop writes, back up, migrate, switch traffic,
or perform production acceptance.
