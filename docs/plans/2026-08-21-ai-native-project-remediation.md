# AI Native Project Remediation Checklist

**Goal:** Close every UI, traceability, session-routing, model, and runtime issue found during real multi-Agent end-to-end acceptance runs across eight materially different business domains.

**Acceptance rule:** An item is complete only after source checks, automated tests, Docker deployment, and browser verification against a real project. Historical failures remain visible as evidence but must not leave active work or ambiguous links.

## Eight-domain real-project acceptance matrix

The previous six-Agent release-control project remains a regression baseline, but it is not sufficient for final acceptance. Final acceptance requires eight new projects whose inputs, collaboration topology, outputs, review gates, and evidence are materially different. A project only passes when its负责人 drives the work from the initial conversation through final acceptance; manually seeding rows in the database is not acceptance evidence.

| # | Domain and public case baseline | Realistic inputs | Required roles and collaboration mode | Required outputs and evidence | Domain-specific acceptance gate | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Software development and incident readiness — Google SRE incident-response case studies and PagerDuty operating model | Product brief, incident scenarios, SLOs, repository, UI constraints, test matrix | Product owner as负责人; UI/UX, frontend, backend, QA, and operations work through a dependency DAG, code review, defect loop, release gate, and Git merge | Working application, architecture and UX specifications, source code, automated tests, deployment/runbook, release note, Git commits, rollback point, exact Run/session/A2A evidence | Application starts in Docker; functional, dependency, offline, security, and rollback checks pass; milestone references every work item, Run, session, file, commit, and approval | In progress — `PulseOps` project `04387e1c-a2d2-4523-9480-4001b6afc54b` |
| 2 | Marketing launch — New Balance Fresh Foam launch case | Product brief, target audiences, channel constraints, creative assets, launch calendar, KPI definitions, measurement assumptions | Marketing负责人 coordinates audience research, creative, copy, channel operations, analyst, and brand/legal review; creative review is iterative while channel setup and measurement run in parallel | Positioning, audience segments, campaign concepts, copy variants, content calendar, channel plan, UTM taxonomy, experiment plan, launch checklist, post-launch report | Every asset has owner, channel, audience, version, approval, and measurement link; claims and brand review pass before scheduling; KPI report reconciles to source data | Pending |
| 3 | Data analysis — Google Analytics Merchandise Store real ecommerce data | Event/ecommerce extract, data dictionary, business questions, time window, quality rules | Analytics负责人 coordinates data engineer, analyst, BI designer, business stakeholder, and data QA; ingestion, profiling, metric design, dashboarding, and reconciliation use a staged data pipeline | Reproducible queries/notebook, cleaned dataset manifest, metric dictionary, funnel/cohort analysis, dashboard specification, insight memo, data-quality report | Metrics reproduce from the supplied extract; missing/duplicate/outlier handling is documented; business conclusions cite queries and data versions | Pending |
| 4 | Human resources and recruiting — GitLab public hiring and scorecard process | Approved headcount, job description, competency rubric, synthetic candidate packets, interview availability, privacy constraints | Hiring manager as负责人; recruiter, sourcer, interviewers, HRBP, and compliance use sequential stage gates, independent scorecards, debrief, and confidential access boundaries | Hiring plan, JD, sourcing brief, interview kit, structured scorecards, scheduling plan, debrief record, decision rationale, candidate communication templates, audit log | No candidate advances without required independent scorecards; confidential artifacts remain scoped; final decision is explainable and bias/privacy checks are recorded | Pending |
| 5 | Business operations — NYC 311 service-request operations | Public 311 request sample, service taxonomy, borough/agency ownership, priority and SLA rules, shift calendar | Operations负责人 coordinates dispatcher, data analyst, field-operations leads, quality reviewer, and public communications; work arrives as an event queue with routing, reassignment, escalation, and shift handoff | Classified and prioritized queue, routing plan, SLA dashboard, backlog/risk report, field work orders, handoff log, exception playbook, public status summary | Every request has traceable classification, owner, SLA clock, handoff, resolution evidence, and reopen path; aggregate counts reconcile to the input sample | Pending |
| 6 | Customer service — Software AG ticket-resolution case on Jira Service Management | Multichannel ticket sample, customer/account context, product/service catalog, SLA and escalation policy, knowledge base | Support负责人 coordinates Tier 1, Tier 2, product specialist, quality reviewer, and knowledge manager; work uses SLA queues, escalation, linked engineering tasks, customer replies, and knowledge reuse | Triage queue, response drafts, escalation links, troubleshooting record, resolution summary, knowledge articles, SLA/CSAT report, reopen handling | Priority and SLA are correct; escalated tickets link to the exact specialist session/work item; customer-visible replies exclude internal notes; resolved cases create or update reusable knowledge | Pending |
| 7 | Finance and procurement — AWS/FinOps multi-cloud cost-governance case | FOCUS-like cost-and-usage extract, account/tag mapping, budgets, contracts/pricing, anomaly thresholds, ownership map | FinOps负责人 coordinates finance analyst, procurement, cloud architect, service owners, and auditor; anomaly events trigger investigation, technical action, savings validation, and financial/procurement approval | Normalized cost model, anomaly report, showback/chargeback allocation, savings opportunities, commitment-purchase proposal, approval record, forecast, realized-savings ledger | Allocations reconcile to source totals; each recommendation states cost, risk, owner, evidence, approval, and realized outcome; purchase commitments require human approval | Pending |
| 8 | Compliance and risk research — NIST CSF 2.0 Organizational Profile | Current/target CSF profile, policies, system inventory, control evidence, risk appetite, applicable requirements | Compliance负责人 coordinates security architect, system owners, risk manager, legal reviewer, and independent auditor; evidence-request loops feed current/target gap analysis and remediation approval | Current/target profile, evidence index, gap and risk register, control mapping, remediation roadmap, exception record, executive summary, audit-ready evidence pack | No control is marked satisfied without evidence; gaps map to owners and deadlines; exceptions include approver and expiry; final profile is reproducible from versioned source evidence | Pending |

### Cross-domain completion rules

- [ ] Each project begins with a user-to-负责人 planning conversation, records the agreed scope, and is explicitly approved before autonomous execution starts.
- [ ] Each project uses at least five active Agents with role-appropriate instructions; no generic “万能研发 Agent” may stand in for a business role.
- [ ] Each project demonstrates a distinct collaboration topology from the matrix, and all wake-ups are targeted `@` mentions or durable directed work—not broadcast fan-out.
- [ ] Every work item, Run, A2A message, group message, file, Git commit, milestone, approval, and audit event resolves to the exact session and message anchor that produced it.
- [ ] All project sessions use the same standard Web Chat renderer, context construction, compaction, recovery, tool rendering, and durable history path.
- [ ] Removed Agents remain visible in history but cannot receive new messages or perform project actions.
- [ ] Lists use the shared, business-agnostic, i18n-enabled pagination component and preserve page/filter/selection state in the URL.
- [ ] Light/dark themes use shared tokens; every project workspace tab and every relevant detail state receives browser screenshot review.
- [ ] A failed check creates a remediation item, is fixed in source, is verified in Docker, and is rerun in the affected project plus the cross-domain regression suite.
- [ ] Final sign-off requires eight completed projects with no queued/running residue, no unresolved blocker, no ambiguous session link, and a verified Git rollback point for every project.

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
