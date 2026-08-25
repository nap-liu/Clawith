# Conversations, turns, and channel delivery

## Shared execution model

Web, IM, A2A, trigger, task, webhook, and MCP-facing message paths should delegate model/tool work to `call_llm` / `call_llm_with_failover`. New entry points may adapt context and delivery, but must not fork a private tool loop.

Turn execution is logically independent of a socket or webhook request. A transport can disconnect after accepting input; the turn still persists its outcome and delivery state. Process restart recovery needs explicit durable completion/sequence state and must not infer completion only from `created_at`, because PostgreSQL transaction timestamps can sort a final row before independently committed tool rows.

## Sessions and identities

- P2P: the counterpart is stable for the session, so identity is session-scoped.
- Group: senders vary per message, so identity is message-scoped.
- A2A: `(min(agent_a, agent_b), max(...))` is the normalized pair. `ChatMessage.agent_id` can therefore be the smaller UUID for both directions; load A2A history by `conversation_id`.
- Trigger/A2A/background turns can carry a creator `user_id` for execution context. They do not thereby inherit that creator's administrative read authority.

Session introspection is always an owned-session subset. Human Web/IM access uses authoritative user permissions; non-human access is limited to the agent's own A2A, trigger, and current-session context. Denials should not leak whether another session exists.

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

Recall state is explicit (`available`, `recalling`, `recalled`, `partial`, `expired`, `failed`, or `unsupported`). Claim a recall attempt in a short transaction, perform network I/O outside the database lock, then merge results only if the attempt still owns the claim. Provider batch APIs must parse per-item results, not equate HTTP 200 with success.

Fully recalled messages remain in the audit trail and render as a tombstone. LLM history must not replay recalled content as if the recipient still saw it.

## Known channel boundaries

- IM model execution has no whole-tool-loop timeout; request-level model timeouts and tool-round limits belong in the shared core.
- Native provider capabilities differ across P2P and groups. Normalize the lifecycle result while keeping provider-specific request semantics in adapters.
- `send_channel_file` has historically had stronger native file coverage on some transports than others. Capability absence must be explicit and a safe link fallback may be used where product-approved.
- Web live monitoring is distinct from write permission. A read-only viewer may receive events, but server-side writes remain denied.
