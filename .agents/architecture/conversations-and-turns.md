# Conversations, turns, and channel delivery

## Shared execution model

Web workspace references carry canonical Agent-relative paths, including core
and daily memory, through shared attachment validation. A live file draft is
preview content until its write completes; it must not become a required stored
attachment during that write. Validation still requires an existing file under
the Agent's allowed workspace roots and rejects private or escaping paths.
Rejected sends reconcile optimistic composer state with the current server
turn snapshot without stopping an active turn or applying a stale generation.

Web, IM, A2A, trigger, task, webhook, and MCP-facing message paths should delegate model/tool work to `call_llm` / `call_llm_with_failover`. New entry points may adapt context and delivery, but must not fork a private tool loop.

Native model/tool invocations run in a supervised, single-use process through
`services/agent_execution`. The existing shared loop, tools, schemas, workspace
routing and channel adapters remain authoritative. Nested calls within one root
Turn stay in that process; independent Turns get separate processes. Direct confirmed
tool execution uses the same boundary. File search has no additional process or
changed matching/truncation contract.

The supervisor retains the conversation execution lease, provider concurrency
slots and ingress cancellation target. Awaited callbacks use a private inherited
socketpair, preserve callback ordering and receipt mutations, and inherit only
their own active turn. A nested A2A call may inherit its parent's still-held
Session lease; unrelated tasks must acquire their own lease. Children create
independent database pools and never start connectors, schedulers or the
ASGI bootstrap. Existing workload admission remains outside the child boundary.

The isolation boundary adds no admission quota, execution deadline, memory or
CPU restriction. Existing workload/provider limits and tool behavior stay in
force. An independently executing Agent cannot hold another Turn's Python GIL
or ingress event loop. Shared host capacity, storage, provider quotas and the
external sandbox remain the existing platform dependencies.

An unexpected child exit during an admitted durable Turn raises
`TurnInterrupted`; entry points preserve that anchor for the shared recovery
owner instead of writing a model failure. Invocations without a durable anchor
retain the normalized `agent_execution_unavailable` failure. Neither outcome
starts model failover. Explicit cancellation reaps the child process group, and
Linux parent-death signalling prevents orphan execution after supervisor exit.
`AGENT_EXECUTION_ISOLATION=1` enables the boundary; the switch can be disabled
for rollback or in-process contract tests.

Independent background work is acknowledged by the supervisor before the
originating Turn can exit. It uses the existing task/schedule/wake/recovery
executor and a fresh root context. Nested durable anchor admission and terminal
stop checks delegate to the supervisor's existing active-turn protocol. A child
database commit remains inside that protocol's admission gate, so a stop cannot
miss an A2A anchor committed concurrently with its snapshot.

Build an immutable runtime/model snapshot and end the inbound read transaction
before waiting on provider capacity or entering a long tool loop. Usage, tool
results, delivery state, compaction, and other externally relevant progress use
purpose-specific short transactions. A slow provider must not leave the ingress
session idle in transaction.

Model generation settings use one precedence rule across foreground and
background work: execution override, then Agent override, then model default.
Subagents, tasks, schedules, and triggers persist optional model and temperature
and reasoning-effort overrides; missing values inherit from the Agent. The UI
names temperature “想象力” and constrains it to the supported 0–2 range.
Reasoning uses the contract in `model-reasoning-controls.md`; in particular,
explicit `none` disables thinking where the selected model supports it and is
not interchangeable with a missing value.

Each durable trigger execution dispatches as an independent invocation. Due
cron and interval occurrences must not be suppressed, merged, or serialized
merely because another recurring execution for the same Agent is pending or
running. Occurrence idempotency prevents duplicate dispatch, shared workload
capacity bounds system load, and workspace locks serialize only conflicting
mutations rather than the complete model turn.

Trigger cancellation disables the reusable definition. Deletion is a separate
operation allowed only after the definition is disabled and no execution is
pending or processing. Deleting the definition releases its name and storage,
while durable execution rows and conversation history retain the original
trigger ID and an execution-time name snapshot. Enqueue and deletion serialize
on the trigger row so deletion cannot create an unclaimable execution. Database
guards preserve the same contract for older binaries during rolling upgrades.

Turn execution is logically independent of a socket or webhook request. A transport can disconnect after accepting input; the turn still persists its outcome and delivery state. Process restart recovery needs explicit durable completion/sequence state and must not infer completion only from `created_at`, because PostgreSQL transaction timestamps can sort a final row before independently committed tool rows.

Each startup recovery task acquires the existing conversation execution lease
before writing recovery state. A busy lease keeps that task waiting while its
original durable origin, anchor, and generation remain current; completion,
STOP, or replacement ends the wait. The original owner may renew normally, and
only its release or lease expiry permits recovery. Waiting never holds a database
transaction, steals a lease, or blocks recovery of another conversation.

### Durable background admission and recovery

Platform-hosted Trigger, Task, Schedule, heartbeat and oneshot executions persist a real
`ChatSession`, input anchor, generation and business reference before dispatch.
Manual execution and auto-executing task creation commit this admission before
returning acceptance. Background sessions reuse the non-human `trigger` source
with no Session `user_id`; the anchor preserves the execution principal without
attributing the input to a human sender. `background_execution.kind` identifies
the business adapter rather than introducing new channel identities.

Automatic Task creation saves the Task, its TaskLog and its turn anchor in one
transaction. Context preparation happens before inserting the Task; Web, IM,
API and tool entry points cannot acknowledge or dispatch a half-admitted task.

`background_execution` stores the existing business reference, model and memory
settings, completion data, and finalization/delivery markers. Task runs use
`TaskLog.id`; a Schedule occurrence has a stable identity derived from that
schedule and its due time. Its anchor and schedule counters/next due time commit
atomically. Recovery continues that occurrence without resetting the schedule
or creating a second run. Fixed task prompts may be preserved through the shared
`prepared_turn_context` option; adapters do not own separate model/tool loops.

`run_background_turn` uses `resume_startup_anchor` and `resume_turn` for both
initial execution and recovery, then the ordinary channel/LLM caller. The
conversation lease covers recovery writes, tool materialization, model execution
and finalization. Workload capacity is acquired once by the execution owner;
native Gateway admission must not retain another permit around this path.
Foreground Web/H5, IM and MCP keep their transport behavior and ordinary
admission while converging on the same durable recovery core. Subagent/project
execution retains its existing business claimant; the generic scanner excludes
subagent Sessions. Deterministic supervision reminders remain delivery operations.

Normal and recovered background completion share the same per-kind finalizer.
Ordinary trigger completion summaries are platform history and WebSocket updates
only, even when the trigger was created in an IM conversation. Neither initial
completion nor recovery sends those summaries through an IM provider. Explicit
message/file tools retain their delivery, as do exact `on_message` origin
continuations and A2A callbacks. Historical summary receipts do not authorize
recovery to introduce an additional external send.
The terminal reply, business state and finalization marker commit together;
later reconciliation only finishes missing business writes or delivery. Trigger
completion owns webhook consumption; Task completion owns task status/logs;
Schedule completion owns successful manual counters; oneshot completion owns
its notification; heartbeat completion owns its activity record. Heartbeat
admission atomically saves the input and consumed inbox snapshot with its
occurrence timestamp. It has no independent model/tool loop.
STOP and service interruption do not consume a webhook batch.
A stopped Task may be made available for an explicit new run, but its cancelled
anchor is never automatically reopened. Durable failed replies preserve their
typed failure code when returned to callers.

Startup and the existing trigger worker tick share recovery discovery. Current
Session Turn pointers, unfinished background completion and pending terminal
delivery are authoritative; new durable turns have no age-based recovery cutoff.
The three partial indexes `ix_chat_sessions_active_turn`,
`ix_chat_messages_background_unfinished` and `ix_chat_messages_terminal_pending`
support those queries. This adds no table, execution engine, recovery retry
count, recovery deadline or source restriction. Existing provider and workload
policies remain authoritative. Waiting for confirmation or project runtime
availability must not be bypassed by making another execution.

Losing a durable model invocation's conversation lease raises the same
`TurnInterrupted` signal as process loss. It must not become a business failure
or allow an old owner to write the new owner's terminal result. Confirmation
continuations use the original anchor's execution identity and completion owner;
background continuations cannot bypass their business finalizer to send a second
reply. A2A confirmation storage uses the canonical Session owner while execution
and reply attribution retain the actual peer Agent.

The rollback snapshot helper reuses these same discovery and execution owners
for only its recorded root IDs and generations, including terminal delivery
tails. It reports business failure, unresolved confirmation and incomplete
finalization explicitly. Its scoped invocation does not start a promoted Turn
outside that snapshot; ordinary application completion still starts the next
Turn by default. This helper does not make legacy background workers compatible
with the new admission metadata.

This recovery change covers platform-hosted execution only. External OpenClaw
runtime and recovery protocols are outside this iteration; native recovery does
not execute their work locally. A native employee receiving a Gateway message
is still covered by the platform's ordinary durable execution contract.

A provider response that sends no bytes within the selected model's request
timeout ends as `model_response_idle_timeout`. This failure never triggers an
automatic retry or fallback. The shared LLM boundary returns one string-
compatible typed failure; each existing workload finalizer records its own
failed state and every user-facing channel renders the same localized failure
message. The LLM client never writes conversation or workload terminal state.

A transient provider HTTP 429 uses one shared recovery lane: the original
request plus at most five identical-payload retries with 1/2/4/8/16-second
backoff. Each retry emits transient status rather than model text. The lane
does not stack with 5xx recovery or model failover, and authentication,
billing, or hard-quota failures are not retried. Exhaustion is a typed terminal
failure that tells the user to wait and send `/continue` in the same Session;
resetting the conversation is neither required nor recommended.

Quota classification follows the actual provider endpoint, not the model's
display provider. On DashScope endpoints, `insufficient_quota` and
`Throttling.AllocationQuota` denote TPS/TPM throttling and enter the same bounded
429 retry lane, including third-party models hosted there. The generic
"plan and billing details" message does not prove billing exhaustion. Explicit
billing/hard-quota evidence and authentication failures remain terminal; other
providers retain their quota semantics. Retries preserve completed tool results
and never replay an already executed tool or a request that emitted progress.
See the provider's [error-code contract](https://help.aliyun.com/zh/model-studio/error-code).

Web and IM `/continue` explicitly reopen only the current Session's last failed
owner when it has a durable typed LLM failure. The original anchor, instructions,
attachments, and completed tool results remain intact. The Session lock admits
one continuation and advances its generation/revision; active, suspended,
cancelled, completed, archived, and superseded turns are not reopened. Ordinary
lifecycle transitions still reject terminal-to-running changes. The explicit
claim and failure audit annotation commit before scheduling the shared durable
recovery lane. Startup recovery can pick up an admitted continuation after a
restart. Continued failure notices and command replies remain visible in audit
history but are excluded from provider replay and terminal-completion detection.
Before each ordinary native Web turn, reload its durable history prefix after
admission; a connected socket's cached conversation may predate asynchronous
continuation, tool results, or completion.

Model text emitted in a response that also carries tool calls belongs to that
intermediate tool round. Persist it with the tool-call audit record and replay
it to the provider unchanged, but do not concatenate it into the terminal
assistant reply. The terminal reply contains only completed plain-text response
rounds; plain-text output durably completed before a late user interjection may
remain a separate terminal segment. This separation preserves raw model and
tool history without exposing repeated tool narration as the final answer.
Legacy terminal rows that already contain an exact copy of their same-turn
durable tool narration remain unchanged in storage and UI history; provider
replay projects only their terminal tail so old sessions stop reinforcing the
obsolete merge behavior.

External IM transports may project non-empty `assistant_content` as one
independent progress message per tool round. The existing tool-call row remains
the single durable source and delivery anchor; the projection must not create a
second assistant history row. Empty and repeated text is suppressed, and final
assistant delivery remains a separate message. Web and transports with an
updateable streaming card may use their existing transient projection.
The Agent-level `im_thinking_output_enabled` compatibility setting controls
this public progress projection; despite its legacy name, it never authorizes
delivery of provider reasoning or hidden chain-of-thought.

### Asynchronous Subagent events on parent turns

A child session becomes viewable as soon as creation commits, including during
a synchronous parent tool wait. The shared tool caller persists and broadcasts
one additional `running` marker with a stable `session_ref` under the same tool
call ID. It retains the round/recovery context and obeys the current turn fence.
The reference is display metadata, separate from the final model-facing result;
history/live projections and stopped results retain it. Existing session access,
read-only details and status subscriptions apply. Synchronous execution still
waits for completion, and retry admission reuses the original child session.
Recovery folds running updates by tool call ID and retains the reference in its
single interrupted result; its existing automatic-replay policy is unchanged.

Ordinary Web, IM, trigger, and non-project A2A parent Sessions use one durable Subagent-event drain rather than one wake turn per child event. A child `ChatMessage` with `subagent_wake=true` remains the notification source, and its parent projection is idempotent through `external_event_key=subagent-parent:<child_message_id>`.

- If the parent is idle, the dispatcher groups a bounded set of events by parent Session and execution identity. The first projection is the root anchor, later projections carry `subagent_turn_anchor_id`, and the batch calls `resume_turn()` once.
- If a parent turn is running, the shared `before_round` drain binds new projections to that active anchor so the next model iteration sees them without enqueuing another turn.
- A terminal assistant row for the root anchor completes every projection bound to that root. Startup recovery resolves a latest injected projection back to the root anchor.
- Project Group/A2A and Leader batching retain their dedicated collaboration semantics.

Do not add a second inbox table, a completion-cohort state machine, or channel-specific copies for this behavior. Preserve per-event audit rows, unique projection keys, bounded batch size, execution-identity validation, and the shared LLM turn loop.

### User messages during a running turn

WebChat (PC/H5) and IM use the same durable turn inbox on existing `ChatMessage`
rows. The WebSocket turn driver accepts ordinary messages while listening for
stop/disconnect; other sockets use the same admission function. Client message
IDs deduplicate retries. Admission checks current Session ownership and keeps
the active anchor's execution identity, model, reasoning, and scene snapshot.

The shared bounded `before_round` drain consumes messages for the same executing Agent at the
next model iteration. Messages arriving too late are promoted after terminal
commit through the existing durable resume path. Stop cancels pending inputs;
socket disconnect alone does not cancel execution. Each new Web user root reloads
durable history so consumed follow-ups survive subsequent turns and reconnects.
Hidden onboarding roots retain their initialized context because ordinary history
intentionally excludes those anchors.
PC/H5 allow sending during generation while retaining the stop action and the
current stream. Read-only, confirmation, quota, and attachment checks still
apply. Gateway-managed OpenClaw turns retain their existing admission behavior.
This capability is independent of automatic scene selection and adds no table,
queue service, or separate model loop.

## Automatic scene selection

Scene drafts and published revisions contain optional `auto_activation` settings
with selected private/group conversation identities. These settings use the
existing scene JSON and publication lifecycle; there is no separate binding
table, activation worker, or execution loop. Publication and rollback lock the
Agent before checking that a conversation has only one enabled automatic scene.
Omitted settings from older clients and the management tool preserve the current
selection.

The management picker deduplicates historical Sessions before pagination.
Platform private targets use tenant, Agent, channel, and user; IM targets also
include the provider conversation, channel configuration, and installation scope.
Project conversations use project scope and require an enabled Agent membership and
the manager's project edit access. Tenant and current target ownership are
validated on save and publication. Runtime resolves the latest Session for the
target; an explicitly opened historical Session is never silently redirected.
Archived IM routes and terminated Sessions cannot activate an automatic scene.

Web, IM, and project-member dispatch share the same scene resolver. Explicit
scene selection takes precedence; `/scene off` suppresses automatic selection
for that Session, while a new Session inherits the target's published setting.
Automatic activation emits no welcome, confirmation, or extra LLM invocation;
this also suppresses the Agent's default welcome fallback on empty Sessions.
Provider reply/@ policy stays with the existing channel adapter. Incoming turn
anchors store the existing scene key/revision snapshot; same-turn IM messages
retain the active anchor's scene snapshot. Recovery reads that revision.
Browser manifests expose menus through the existing least-privilege projection
and exclude automatic-selection targets and system prompts. H5's implicit
default landing remains a fallback after automatic selection; an explicit scene
URL retains precedence. WebChat in-turn admission is a separate capability.

Private platform Session creation, default selection and primary deletion share
one transaction-scoped advisory lock keyed by Agent, user and channel. Acquire
it before selecting or creating the newest primary; release the transaction
before model or provider waits. Concurrent admission must neither create two
primaries nor return a unique-constraint failure.

## Sessions and identities

### Agent group member policies

Agent settings include a Group policies tab organized by group, with group-name
search, channel filtering and visible channel labels. Each group owns multiple
named, independently enabled allow/deny rules. Rules select organization members
or all members. Enabled denies win; the presence of any enabled allow rule closes
that group's roster to unmatched members. With no enabled allows, unmatched
members keep existing conversation permissions. No rule grants identity, Agent,
tool, or native mention permissions. Unidentifiable senders fail closed whenever
that group has enabled rules. With no enabled rule for the exact group, admission
only maintains local stable group/typed participant metadata and delegates identity,
commands, downloads and dispatch to the ordinary adapter path. It performs no
provider lookup, canonical User creation or prepared-actor override. A concurrent
first policy save serializes with that admission through the group row lock.

Group policies and scene activation share `conversation_candidates` as their
authorized existing-conversation source. Group policy selection restricts it to the
Agent's connected IM group channels and excludes projects/private chats.
Its projection resolves stable parent groups before ranking, counting, searching
and paging, so multiple thread Sessions never create duplicate group choices.
Parentless historical threads are excluded before totals and pagination.
Command-control Sessions are not independent group choices; the shared picker
excludes them along with archived and terminated routes. The group policy UI
does not use `agent_groups` discovery rows as its directory and does not wait
for another inbound event. Listing/selecting existing groups is read-only.

Scene target references are revalidated against the current authorized
conversation, then projected into the stable provider group identity. Before a
policy is saved, its group and legacy participant references are deterministic virtual values;
only an explicit save or authenticated ingress materializes their rows. No
historical Session or message is copied or rewritten. Existing rule IDs survive
Session reset and ordinary inbound discovery racing the first settings save.

`agent_groups` owns its rule JSON and optimistic revision. Its stable identity is
Agent, channel, installation scope, and the provider's opaque group reference.
Names and Session IDs never determine policy ownership. Group references are
internal: the management UI/API does not expose the native reference or offer
manual ID registration. Secret rotation preserves scope; a different installation
starts independent groups with no rules. Tenant ownership is checked separately.
Teams channel messages use `channelData.channel.id`; Discord threads use their
parent channel. Exact reply routes stay with transport adapters, following the
[Teams conversation](https://learn.microsoft.com/en-us/microsoftteams/platform/resources/bot-v3/bot-conversations/bots-conv-channel)
and [Discord thread](https://docs.discord.com/developers/topics/threads) contracts.

`agent_group_members` records observed participation using typed provider subjects
within one group. It is not another person directory or a complete provider member
sync. Each adapter supplies the authenticated sender subject and available display
name. Existing scoped ChannelUserBinding rows provide canonical names and verified
aliases; nickname equality never establishes identity. All known aliases participate
in rule matching, so changing between already-bound subjects cannot evade a deny.
Provider subjects and internal member references are not displayed to users.
The UI reuses `OrgMemberAccessPicker` in members-only mode and the standard Agent
permission directory endpoints, including department browsing, search and paging.
New selections persist canonical tenant `User.id` values in each rule's `user_ids`;
members need not have previously spoken in the group. Ingress prepares the complete
authenticated sender profile and uses the shared channel User resolver, including
first directory bindings and verified provider aliases, before matching these IDs.
Legacy alias matching is scoped to the same tenant/provider/channel/installation.
No directory display name or Session owner establishes identity.
Legacy `member_ids` rules remain enforceable. Reads project uniquely bound legacy
selections into organization users without writing; unbound selections remain
visible/removable until a manager replaces them. Explicit saves normalize selected
users while retaining unresolved legacy subjects and the full before-state audit.
The historical participant projection remains only for validating legacy references.
Resolved participants are still observed when denied; an all-member deny needs no
identity preparation. Unresolvable/conflicting identities are rejected.
Available Session group names remain display projections only.

Authenticated Feishu, DingTalk, WeCom, Slack, Discord and Teams ingress converges
on `group_ingress_allowed` before commands, media downloads, remote quote parsing,
message triggers and model dispatch. An enabled all-member deny short-circuits;
other enabled rules consume a complete prepared canonical sender. Feishu contact fields
augment event IDs, never erase them, and conflicting IDs are refused. DingTalk
fresh directory claims are gathered before canonical reconciliation. Both adapters
revalidate configuration after provider waits and before writing identity bindings.
Identity preparation uses independent transactions and holds no group row lock or
open database read transaction while waiting for providers. Final admission locks
the group briefly and rereads its current rules, including edits made during waits.
Denied inputs may establish verified identity bindings and group/member metadata;
no ChatSession or ChatMessage is created. Transport
acknowledgements keep native behavior (Discord slash rejection is HTTP 403).

`PreparedSender` is reused by downstream text/file processing instead of running a
second identity resolution with different claims. DingTalk Stream prepares within
the scheduled work, before references/media, and explicitly passes the actor into
the platform handler. Private chats continue through their ordinary resolver.
Discord Gateway filters mentions before preparation, marks guild Sessions as groups,
and binds the verified parent reference; real inbound traffic upgrades legacy rows.
Historical Discord guild routes cannot bypass confirmation checks as private chats.

Group upserts, member observation and policy saves serialize on the same group
row in short transactions. Different groups do not share a policy lock. Saves
validate selected organization members against the Agent's tenant and active users,
and legacy member references against the group, with manager authority,
check the expected revision and append AuditLog atomically. Provider waits and
model loops do not hold these transactions. There is no rejected-message inbox,
replay worker, generic rule engine or separate execution loop.

Session reset preserves the group reference in validated `im_config` context.
Confirmations for a known group with no enabled rules retain the ordinary path,
including after installation configuration removal or rotation. Confirmation
resumes for restricted groups check the clicking user's canonical identity and legacy scoped bindings, never the
Session's placeholder owner. Legacy Sessions without a stored group reference resolve the same stable native
route on read. When a transport's old Session cannot prove the group identity
(for example an old thread with no parent metadata), confirmation fails closed
while that channel has enabled rules; no historical metadata is backfilled. Already admitted work and ordinary
recovery keep their lifecycle. Private chats, project collaboration and proactive
notifications are outside this inbound gate.

The `group_member_rules` migration supersedes the unpublished Agent-wide roster
candidate. It archives previous modes/rosters and converts each already-known
restricted group into an all-member deny rule. There is no longer an Agent-wide
restriction on future unknown groups: newly discovered groups start with no rules.
Group/member policy data cannot be downgraded to the old evaluator while any rules
remain. This conversion must be reviewed as a product semantics change before any
future production release; this iteration only uses isolated local databases.

Responses uses the existing shared LLM/tool loop with a true SSE adapter. Text and
tool-argument increments may update the UI while streaming, but only a successful
terminal response authorizes execution of returned tool calls. Failed, incomplete,
cancelled and interrupted responses do not silently become successful tool rounds.
Original output Items are attached to the existing message audit chain and replayed
under their matching endpoint/model binding; see `context-and-memory.md`.

- P2P: the counterpart is stable for the session, so identity is session-scoped.
- Group: senders vary per message, so identity is message-scoped.
- While an IM turn runs, all senders addressing that employee share its FIFO
  conversation inbox. A different sender does not create a separate turn.
  Preserve each message's sender attribution, attachments, and durable row.
  Admission refreshes the locked session snapshot. The active turn consumes
  pending conversation input, including older unconsumed backlog, and records
  its current anchor/generation on delivery. Cancellation and ownership still
  fence the consumer; opposite A2A directions target different employees.
  Record consumption time separately from immutable arrival time. Model history,
  recovery and compaction use one shared consumption-order projection, so old
  backlog cannot precede its own root or be compacted out of the active turn.
  If the provider reports that the protected active turn already exceeds its
  budget, recovery may summarize only the contiguous, typed tool-round prefix
  whose final call states are all terminal. It stops before any injected user,
  ordinary assistant, malformed, unknown, or open tool row, preserving that
  row and the entire remaining active tail exactly.
  Pending inputs are excluded from compaction until consumed. Fresh compaction
  reads refresh ORM state after provider waits.
- A2A: `(min(agent_a, agent_b), max(...))` is the normalized pair. `ChatMessage.agent_id` can therefore be the smaller UUID for both directions; load A2A history by `conversation_id`.
- Trigger/A2A/background turns can carry a creator `user_id` for execution context. They do not thereby inherit that creator's administrative read authority.

Session introspection is always an owned-session subset. Human Web/IM access uses authoritative user permissions; non-human access is limited to the agent's own A2A, trigger, and current-session context. Denials should not leak whether another session exists.

Aware execution-record details load the linked Session's complete persisted
history through cursor pagination, including its original task instructions.
A failed page is shown as an incomplete load with retry, not an empty or
complete record. Execution completion refreshes the history; an older in-flight
read must not replace the newer result. This is an audit view and does not
change model context loading or task execution isolation.

Platform administrators in the active tenant context, tenant organization
administrators governing standard Agents, and Agent administrators with manage
access can audit every non-project Session for that Agent. Group membership is
not an additional condition for this governance view; tenant-safe Session edges
remain mandatory.

Active-turn listing and cancellation use one registry across Web, IM, MCP, A2A,
trigger, task, and recovery entry points. Ordinary users can see/control only
their own authorized turns; platform-wide authority must be explicit and
server-enforced. Completion and cancellation serialize against the same durable
turn identity so a late completion cannot overwrite a successful stop.

Confirmation is a suspended ordinary tool call, not a parallel conversation
protocol. Resume it through the original durable Session and source channel,
preserve the assistant/tool ordering, and let channel adapters render buttons or
cards. A confirmation response must not invent a new session or bypass the
shared loop.

The shared suspension writer commits before exposing a confirmation card.
An empty reply from that invocation therefore projects the persisted Session
snapshot; a transport finalizer must not write suspension again. A user can
already have resumed or stopped the same anchor while its earlier invocation
finishes. Web suspension events retain the current anchor, generation and
revision and are emitted only while that same generation remains suspended.

Session STOP uses the execution owner's existing admission gate to cancel every
registered durable anchor, including ordinary A2A Sessions, before interrupting
that root. Web, IM, the control bus and MCP share this stop commit protocol;
interrupting a process alone does not complete the durable cancellation.

Suspension and external completion are generic tool-loop capabilities, not
confirmation-specific behavior. When an external actor fills a suspended tool
result, provider history must still end with the matching
`assistant(tool_call) -> tool(result)` pair. Runtime context must not append a
synthetic user message after that result; otherwise the continuation becomes a
new user turn instead of resuming the suspended tool call.

## Normalized outbound delivery

One persisted outbound `ChatMessage` is the local lifecycle anchor. Ordinary replies and control text use an assistant row. A tool-created file or media artifact may reuse its exact outbound tool-call row, avoiding a second outbox model and preserving one idempotency key. Each durable externally visible outbound operation attaches a normalized delivery receipt to message metadata:

```text
delivery
  channel
  status: pending | sent | partial | failed | unknown
  parts[]
    transport
    remote_message_id / provider keys
    status
    recall capability and recall status
```

One logical message can have N parts because providers split text, cards, or attachments. Recall, delete, edit, and delivery diagnostics operate on these parts through a transport adapter registry. A channel name alone is insufficient when one channel supports multiple transports (for example, provider OpenAPI versus a temporary webhook).

Commit `pending` before provider I/O, append each confirmed part immediately, and finalize to `sent`, `partial`, `failed`, or `unknown`. Commands, ACKs, welcome/background notifications, files, media, and confirmation artifacts do not get channel-specific persistence rules. Provider typing indicators and reactions are transient control-plane state rather than durable messages; they are cleaned up by their own bounded lifecycle and are not exposed as recallable chat content.

DingTalk command replies remain ordinary persisted assistant messages. The
normalized delivery marks them as plain text, so the OpenAPI adapter uses the
provider text template and preserves intentional newlines. Normal model replies
retain Markdown transport semantics.

Recall state is explicit (`available`, `recalling`, `recalled`, `partial`, `expired`, `failed`, or `unsupported`). Claim a recall attempt in a short transaction, perform network I/O outside the database lock, then merge results only if the attempt still owns the claim. Provider batch APIs must parse per-item results, not equate HTTP 200 with success.

Fully recalled messages remain in the audit trail and render as a tombstone. LLM history must not replay recalled content as if the recipient still saw it.

## Known channel boundaries

- IM model execution has no whole-tool-loop timeout; request-level model timeouts and tool-round limits belong in the shared core.
- Native provider capabilities differ across P2P and groups. Normalize the lifecycle result while keeping provider-specific request semantics in adapters.
- DingTalk group-mention cards keep their conversation preview to at most 40
  Unicode code points, including any truncation marker. This caps even all-emoji
  previews at 80 UTF-16 units and leaves delivery headroom. Only the preview is
  shortened; the message body and native mention targets remain complete.
- A native mention in an already-authorized exact group Session does not require
  an Agent-to-human relationship. Individual targets still use canonical tenant
  user IDs and must resolve to one active endpoint for the Session's provider
  installation; proactive person delivery retains its relationship gate.
- `send_channel_file` resolves canonical users through transport adapters. A
  DingTalk user route reuses or creates the canonical P2P Session and then uses
  the exact-Session DingTalk file sender. Capability absence on other
  transports must remain explicit; a product-approved safe link fallback may
  be used where native upload is unavailable.
- Web live monitoring is distinct from write permission. A read-only viewer may receive events, but server-side writes remain denied.
- A durable message from a human always continues through the ordinary
  conversation lifecycle. An `on_message` trigger may observe and enqueue work,
  but it must never consume the human message or suppress its terminal reply.
- Reaction/thinking anchors move only when an inbound message is actually
  consumed by the running turn. A merely pending interjection must not steal the
  visible anchor.
- Web and H5 normalize completed assistant/tool content from the same durable
  message contract. Channel-specific card rendering must not create duplicate
  assistant text or empty presentation bubbles.

## User reference snapshots

Optional `external_context` is ordinary user-message JSON metadata shared by
OpenAPI interaction activation and H5 host-assisted sends. Its presence includes
null and false. The shared projection appends it as user reference material for
initial input, history/recovery, active-turn inbox drain and gateway delivery;
it must not be promoted to system instructions or silently dropped on one path.
Message serialization preserves it and the client message ID for live/history
reconciliation. Context-bearing retries compare question, attachments and JSON
before side effects on all duplicate paths, including session-lock and unique-key
races. Ordinary messages without context retain their existing semantics.
The opt-in browser lifecycle and SDK contract are owned by `openapi.md`.

WebChat and H5 render these snapshots through the same reference-data disclosure.
Web history selection, pagination, recovery and live events use the shared message
projection; transport-specific field allowlists must not drop `external_context`.
The product label is “Reference data” (参考资料), with the source name and the
original snapshot available on expansion. It describes data attached at send
time, not a live view of the host page.

## Media execution in child sessions

Children created by an admitted human IM turn retain that exact sender's
tenant identity and the server-validated origin Session/input anchor. This
session-scoped authority applies to both ordinary subagents and media children;
it grants no general access to a private Agent. Admission verifies the current
running anchor, and execution/recovery revalidates the real human origin and
active User/Identity. A P2P origin must also belong to that exact sender;
group origins retain per-message attribution. Nested media work inherits the
same origin. Web, other users' P2P sessions, synthetic inputs, other tenants,
and unattended tasks retain their existing
Agent-access rules; caller-supplied metadata cannot grant origin authority.

The media executor reuses Session/input anchor/Turn/SubagentRun, leases, capacity,
recovery and parent events. `task_id` is the individual input anchor; `session_id`
is the reusable child session. Inputs remain separate FIFO jobs instead of being
merged into a live Agent round. Each completed input publishes its own result and
attachments; native media delivery targets the original parent conversation.
Media sessions retain their original parent-notification mode across follow-up
turns, including enterprise model tests created with `notify_parent=False`.
A parent that is itself a subagent receives completion through its existing inbox
and leased worker, preserving scene/causal metadata rather than starting an
ordinary Web/IM turn alongside that worker.

`stop_subagent` accepts an optional media task ID. Cancelling a queued job affects
that input; stopping a running media job fences its lease and lets later queued
inputs run. Stopping a parent turn cancels causally owned nested media inputs too.
The session detail API accepts `task_id` for historical per-task status; ownership
checks remain identical to ordinary child-session access. The shared task card
uses that status, so an older task does not change when a session continues.

Client serialization projects media-request input references into the existing
attachment contract, including historical tasks, without exposing their encrypted
connection snapshot. The shared playback authorizer recognizes these exact
references while preserving Session and Agent access checks. Original third-party
URLs are rendered directly without platform credentials; local files retain the
standard download/playback routes. Session viewers reuse the shared image/media
components, and nested image previews render above the viewer and own Escape/focus.
