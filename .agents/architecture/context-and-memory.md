# Context, compaction, and agent memory

## Budget authority

Context decisions use the selected model's configured capacity and ratio:

```text
effective prompt budget = floor(context_window * context_usage_ratio)
```

Background resources may explicitly omit Soul and/or memory when building an
Agent context. Tasks, schedules, triggers, and Subagents share the same
`include_soul` / `include_memory` semantics; both default to enabled so legacy
resources retain their prior behavior.

Published scenes expose independent `include_soul` and `include_memory`
switches, both enabled by default. Their immutable turn revision controls the
same shared context-builder flags in Web and IM. Disabling loading does not
delete workspace files or remove previously persisted conversation history.

Provider-reported usage is the authority for successful model rounds. Do not
replace token accounting with character heuristics or a stale model-family
lookup. Primary/fallback dispatch must preserve a safe protected history suffix
for every model that may serve the turn.

The HTTP client bounds connect, write, pool establishment, and consecutive
response-read inactivity with the selected model's request timeout. The read
timer resets whenever provider bytes arrive, so it is not a whole-response or
whole-tool-loop deadline. A response-idle timeout is a non-retryable model
failure: the platform does not switch models, lower reasoning effort, or replay
the turn. Whole tool-loop timeouts do not belong in channel adapters.

## Turn partition and compaction

- The current user turn—including its assistant/tool rounds—is an atomic
  partition. Never summarize, truncate, or rewrite it as historical context.
- Normal compaction removes only completed older turns and preserves the
  configured recent complete-turn suffix; the supported minimum is three.
- On an explicit provider context-overflow rejection before any streamed,
  thinking, or tool side effect, recovery may retry a finite sequence of
  protected-turn sizes from N down to zero. Each level is attempted at most
  once, the durable current tail is reloaded, and the current input is retried
  unchanged.
- If no older complete turn is compressible, return an explicit no-op/provider
  failure. Never loop recovery indefinitely or discard the active turn.

Compaction is durable and auditable. Materialize and verify the archive before
committing the compaction marker. A model summary must preserve active goals,
completed and incomplete work, evidence, blockers, next actions, and relevant
handoffs. Validation/repair failure falls back to a deterministic lossless
archive reference; summary quality must not block safe compaction or silently
lose history.

Semantic summary generation and repair use the ordinary model output allowance,
without a separate summary token cap or character-length acceptance gate. The
legacy `compact_summary_max_tokens` column remains for schema compatibility.
Structural validation tolerates Markdown emphasis on field labels. The local
archive fallback retains bounded excerpts; that availability path does not
limit or replace an otherwise valid semantic summary.

## Tool-result context

Persist the truthful original tool result. The bounded LLM view of fresh textual
tool output currently allows up to 32K characters per result and 64K in one
round. An over-limit view must say `TRUNCATED` and provide the exact path to the
complete stored content. Non-text/multimodal payloads do not pass through the
text character limiter.

Display/log sanitization must not poison the durable result later replayed to
the model. Exact file paths and stage-specific storage/parser errors remain
visible; do not collapse them into a false “not found” or canned success.

Tool multimodal compatibility is a final provider projection, after canonical
history assembly and compaction. A projected user-role observation belongs to
the preceding tool batch, never a new user turn. Durable tool result resources
are reloaded from their original rows during recovery; they do not depend on an
in-memory cache or a previously generated provider URL.
Replay preserves the saved bounded model text rather than regenerating it from
the full standard result. Projection does not mutate earlier messages or Responses
snapshots. Chat serialization omits empty assistant content on tool calls in both
live and reloaded history, preserving the same wire prefix across tool rounds.
Private MCP metadata and user-only content are excluded from summary input as
well as ordinary inference; Web display uses an independent backend projection.

## Scene prompt blocks

Each scene system-prompt block accepts up to 30,000 characters through the
settings editor, scene API, and seeded `manage_scene` tool schema. Enabled
blocks are loaded in order without per-block truncation; model context budgets
still apply. Raising the input limit does not restore previously truncated text.

## Agent memory loading

Core memory is loaded without an arbitrary fixed-character truncation. Daily
memory loading is configured per Agent for 0–30 recent days; zero disables it.
Memory files must never contain credentials. Project Agents may map their
repository-owned memory layout through the runtime workspace abstraction, but
the context builder presents one normalized memory contract.

This product-level Agent memory is distinct from coding-agent instructions for
this repository. Repository engineering knowledge lives in `AGENTS.md`,
`.agents/`, code, and tests—not in any developer's personal assistant memory.

## Media session context

Media understanding projects completed input/result pairs into native multimodal
messages, ordered by task anchor rather than result arrival timestamps. It uses
`maybe_compact` and the normal compaction-aware history loader, not a parallel
summary store. The immutable model projection comes from the accepted enterprise
model snapshot, including its real ID, protocol and context/output settings.
Legacy accepted tasks retain their original encrypted connection projection.
Provider-reported prompt usage triggers the shared budget logic;
media summary calls use the shared streaming client because Omni requires it.
Pending jobs are excluded from the compaction projection; current and recent turns
remain protected. Summaries retain source references and full archival history;
local media references are signed again for each provider request.

Generation continuation inherits compatible output artifacts and parameters.
The parent Agent supplies the complete current image/video instruction, carrying
forward any desired language constraints from its ordinary conversation context.
TTS always receives the exact current spoken text. A generation prompt never
contains an ever-growing concatenation of prior chat messages. Explicit `files: []`
clears inherited references. Artifact version management is deferred.

## Responses protocol state

Responses output Items are preserved on existing assistant/tool message metadata
as `responses_snapshot`, including the protocol, endpoint and selected model binding.
Only that same binding replays native Items; a changed model or endpoint uses the
ordinary content projection. Opaque reasoning state is replayed without decoding
or exposing it as user-visible text. Message phase and function call identities
remain intact across ordinary tool rounds, confirmations and recovery.

Completed plain rounds checkpoint a pending snapshot on their exact turn anchor.
The existing terminal writer consumes it in the same transaction as the final
assistant row. Pending snapshots are never historical completed output. Historical
compaction continues through `ChatCompaction`; retained complete turns reload their
native Items, while archived turns do not reappear through provider-side state.
The platform remains the conversation authority; no provider conversation store
or parallel context lifecycle is introduced.
