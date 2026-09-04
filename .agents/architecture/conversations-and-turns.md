# Conversations, turns, and channel delivery

## Shared execution model

Web, IM, A2A, trigger, task, webhook, and MCP-facing message paths should delegate model/tool work to `call_llm` / `call_llm_with_failover`. New entry points may adapt context and delivery, but must not fork a private tool loop.

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

Turn execution is logically independent of a socket or webhook request. A transport can disconnect after accepting input; the turn still persists its outcome and delivery state. Process restart recovery needs explicit durable completion/sequence state and must not infer completion only from `created_at`, because PostgreSQL transaction timestamps can sort a final row before independently committed tool rows.

A provider response that sends no bytes within the selected model's request
timeout ends as `model_response_idle_timeout`. This failure never triggers an
automatic retry or fallback. The shared LLM boundary returns one string-
compatible typed failure; each existing workload finalizer records its own
failed state and every user-facing channel renders the same localized failure
message. The LLM client never writes conversation or workload terminal state.

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

Ordinary Web, IM, trigger, and non-project A2A parent Sessions use one durable Subagent-event drain rather than one wake turn per child event. A child `ChatMessage` with `subagent_wake=true` remains the notification source, and its parent projection is idempotent through `external_event_key=subagent-parent:<child_message_id>`.

- If the parent is idle, the dispatcher groups a bounded set of events by parent Session and execution identity. The first projection is the root anchor, later projections carry `subagent_turn_anchor_id`, and the batch calls `resume_turn()` once.
- If a parent turn is running, the shared `before_round` drain binds new projections to that active anchor so the next model iteration sees them without enqueuing another turn.
- A terminal assistant row for the root anchor completes every projection bound to that root. Startup recovery resolves a latest injected projection back to the root anchor.
- Project Group/A2A and Leader batching retain their dedicated collaboration semantics.

Do not add a second inbox table, a completion-cohort state machine, or channel-specific copies for this behavior. Preserve per-event audit rows, unique projection keys, bounded batch size, execution-identity validation, and the shared LLM turn loop.

## Sessions and identities

- P2P: the counterpart is stable for the session, so identity is session-scoped.
- Group: senders vary per message, so identity is message-scoped.
- A2A: `(min(agent_a, agent_b), max(...))` is the normalized pair. `ChatMessage.agent_id` can therefore be the smaller UUID for both directions; load A2A history by `conversation_id`.
- Trigger/A2A/background turns can carry a creator `user_id` for execution context. They do not thereby inherit that creator's administrative read authority.

Session introspection is always an owned-session subset. Human Web/IM access uses authoritative user permissions; non-human access is limited to the agent's own A2A, trigger, and current-session context. Denials should not leak whether another session exists.

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
