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

- Web, IM, trigger, task, recovery, and A2A turns use one shared context
  detection, compaction, durable reload, and retry loop.
- The current user anchor and any open assistant/tool tail are atomic. Closed
  old assistant/tool rounds in a long-running current turn are normally
  protected, but when that protected turn itself exceeds the model budget the
  loop may lower its protected-round floor to zero and summarize those closed
  rounds. It never rewrites the anchor or an open call.
- Normal compaction removes only completed older turns and preserves the
  configured recent complete-turn suffix; the supported minimum is three.
- On an explicit provider context-overflow rejection before any streamed,
  thinking, or tool side effect, recovery may retry a finite sequence of
  protected-turn sizes from N down to zero. Each level is attempted at most
  once, the durable current tail is reloaded, and the current input is retried
  unchanged.
- If no older complete turn is compressible, compact closed rounds inside the
  oversized current turn without a protection floor. Return an explicit
  provider failure only when the remaining anchor/open tail itself cannot fit.
  Never loop recovery indefinitely or discard open work.

Compaction is durable and auditable. Materialize and verify the archive before
committing the compaction marker. A model summary must preserve active goals,
completed and incomplete work, evidence, blockers, next actions, and relevant
handoffs. Validation/repair failure falls back to deterministic recovery material
with a verified archive reference; summary quality must not block safe
compaction or silently lose history. Lossless retention describes the durable
audit history, not the semantic completeness of a bounded fallback summary.

The compaction model request is a special recovery operation and may use the
model's full physical context window (100%); the ordinary configured usage
ratio continues to govern normal Agent dispatch. Provider-reported usage, not
a character estimate, triggers the transition.

Semantic summary generation and repair use the ordinary model output allowance,
without a separate summary token cap or character-length acceptance gate. The
legacy `compact_summary_max_tokens` column remains for schema compatibility.
Structural validation tolerates Markdown emphasis on field labels. The local
archive fallback retains bounded excerpts; that availability path does not
limit or replace an otherwise valid semantic summary.

### Audited continuity optimization

This subsection is the implemented continuity contract. The existing turn
partition, budget trigger, protected suffix, current anchor/open tail, ownership,
recall, and shared recovery lifecycle stay intact.

The objective is a compact handoff that tells the next invocation what the task
is, what has actually happened, and what to do next. There remains exactly one
`ChatCompaction.summary_text`. Do not introduce a goal table, goal-management
tool, independent goal state, background reconciler, or second execution loop.

#### Implementation ownership

The relevant owners are under `backend/app/services/llm/`:

- `compactor_serialization.py` preserves the saved model-visible tool result,
  strips the alternate full audit result, and keeps recalled rows as tombstones.
- `compactor_summary.py` performs structural validation only. Identifier recall
  and sampled objective pinning remain compatibility helpers but are not on the
  runtime generation or repair path.
- `compactor_runtime_support.py` uses the shared streaming client, the ordinary
  output allowance, and accepts only a nonempty terminal response without tools.
- `compactor_runtime.py` labels replacement and retained original history,
  performs one optional structural repair, records semantic/repaired/degraded
  generation mode, rejects stale history or marker changes, and leaves dry-run
  state uncommitted.

These owners correct identified source-level failure mechanisms; they do not
claim that every execution drift or provider timeout is caused by compaction.

#### Reuse the original conversation history

The complete eligible conversation history is the summarizer's source. Reuse
the existing ordinary model-visible history representation and its original
content, order and provenance. Do not add a summary-specific history layer,
action taxonomy, field allowlist, or relevance prefilter. The implementation
cannot predict which actions or intermediate details a future continuation needs;
selection of important facts belongs to semantic summarization, not input code.

Preserve all eligible user/assistant/tool messages, including unfamiliar actions,
failed/cancelled results and uncertain states. If the ordinary history loader
drops a persisted model-visible event because its status/type is unfamiliar,
fix that completeness defect at the shared history owner rather than define a
second summary-only event model. Unknown does not mean successful or irrelevant.
The existing partition still protects open work; this does not make open rows
eligible for replacement.

No summary-specific head/tail clipping or rewriting of user text, tool arguments
or results is allowed. Reuse the saved model-visible tool result and its exact
full-content reference, rather than expanding a different result from audit
metadata. Keep ordinary history's existing folding of updates for the same call;
any correction must use anchor/round plus call identity, never call ID alone.
Do not collapse distinct calls merely because their text is similar.

Retain existing source/sender and consumption metadata so an observation stored
under a user role is not mistaken for a new human requirement. Quoted context,
attachments, task/A2A inputs and Subagent reports keep their original attribution.
Use the existing model-request serialization and access/visibility rules, not a
raw dump of internal objects. Recalled tombstones and media keep their existing
handling. This is reuse of the current security and history contract, not a new
summary-specific field policy or authorization to discard unfamiliar history.

Within that same history, each request labels three roles, not three separate
history pipelines:

1. The prior active summary, labelled as historical and potentially superseded.
2. The complete eligible replacement span from the existing turn partition.
3. The retained original history, including the current anchor and consumed
   active-turn history, in its existing order. Do not select only particular
   action types or user sentences. Pending, unconsumed inbox messages remain
   excluded by the ordinary history contract; open work remains explicitly open.

These are the existing history/partition boundaries, not an additional relevance
filter or arbitrary text clipping. The complete input may still exceed provider
capacity. Do not duplicate the same history item across the three labelled inputs;
preserve source references for folded rows. Reference-only rows
are never marked compacted or counted as replacement coverage. The summary may
briefly reflect their latest goal adjustment and continuation point, but must
not repeat the whole retained suffix. That suffix remains authoritative history.

#### A single semantic handoff

Use four sections in the generated text, with empty sections stated explicitly:

| Section | Required meaning |
|---|---|
| Current objective and constraints | Active deliverables, scope, prohibitions, approval boundaries, and explicit priority across unfinished objectives. |
| Progress and key context | Confirmed completed work, remaining work, decisive evidence, failed approaches and relevant decisions; distinguish observation from inference. |
| Next actions and blockers | The concrete continuation point, prerequisites, blockers and any approval still needed; distinguish the final goal from its next step. |
| Necessary references | Exact paths, identifiers and source locations needed to continue or verify; omit irrelevant identifier inventories. |

The prompt resolves explicit replacement versus addition, keeps unresolved
objectives unless actually cancelled/completed, and interprets a bare “continue”
against the established task. A proposed action is not completed work, a child
report does not replace its parent's task, and uncertainty must remain explicit.
User constraints relevant to permissions or external effects must not disappear
through paraphrase. Source text and previous summaries remain lower-trust input,
not instructions to the summarizer about changing its output contract.

Remove `pin_summary_objective()` from the generation/repair path and stop
mechanically appending every missing UUID, path or numeric identifier. Code may
attach a verified archive reference; it must not replace the semantic objective
with sampled snippets. Keep the existing background `<conversation-summary>`
wrapper and role; do not elevate generated content to system authority.

#### Generation, validation and bounded failure

Use the selected model's shared streaming client for every entry point, including
media, without publishing summary tokens or invoking tools. Preserve its protocol,
headers, timeout and applicable shared generation policy; this optimization adds
no reasoning downgrade, model switch or transport retry policy. Use the ordinary
model output allowance for both initial generation and repair, not
`compact_summary_max_tokens`.

Return content, usage and protocol-normalized completion status. Accept only a
successful terminal response with nonempty text and no unexpected tool call.
Length-limited, interrupted, incomplete or missing terminal responses are not
successful summaries. Use each adapter's completion semantics, not a universal
comparison with one provider's `finish_reason` string.

The normal path makes one logical generation call. A complete draft that fails
the four-section structural contract may receive one repair call. Timeout,
provider failure or incomplete generation does not enter format repair. Online
validation checks structure and usability, not semantic truth through substring
or identifier-recall thresholds; there is no second online judging model.

The first implementation adds no segmented-summary loop. Explicit input overflow
or exhausted generation/repair falls back to bounded recovery material. Never
silently shorten user instructions to make a request appear successful. A later
segmentation change requires local evidence that it is needed and a finite
logical-call budget including repair; shared transport retries must be accounted
for separately. It must not recursively split on timeout, throttling or arbitrary
model errors. Arbitrarily large single inputs cannot be guaranteed to fit.

Build degraded recovery material in deterministic priority order: degradation
notice and verified archive locations; latest effective original instructions;
historical continuity excerpts from the old summary; recent complete results.
Keep source locations and original ordering, mark every omission, and never
claim the whole prior summary fits a bounded fallback. If effective instructions
cannot be resolved deterministically, retain the available recent candidates
with their provenance and state that task resolution is incomplete. Historical
“next actions” are not a fresh execution directive. When authorization, current
goal or execution status is uncertain, the continuation is to inspect the exact
archived source and reconcile it before taking or repeating external action.

#### Archive, compatibility and atomic replacement

Keep original audit rows intact. The model-readable archive is a separate,
access-controlled view: it retains permitted source content and exact original
locations, but excludes the same hidden/private material as the summary input.
Bounded summary tool views must still resolve to their full permitted stored
content. “Full archive” never authorizes exposing raw private metadata to a model.
Materialize and verify recovery references outside a database transaction before
marking source rows compacted. Archive failure leaves the old history active;
do not delete a shared content-addressed archive after a stale candidate fails.

Snapshot the prior marker identity, replacement source fingerprint and ordered
reference history, including relevant source/consumption/recall state. In the
existing short locked commit, revalidate that snapshot and active-turn ownership,
then atomically save the new marker, flag only replacement rows, supersede the
old marker and reset the applicable usage observation. Changed references or a
new active marker invalidate the candidate; use the existing durable reload path,
not a new internal retry loop. Merely arriving unconsumed inbox rows must not
invalidate an otherwise unchanged consumed snapshot. Preserve recall invalidation
and cancellation fences.

No schema migration is required. `summary_validation_passed` remains the loader's
usability gate: a safe archive-backed degraded summary can be `True`; setting it
to `False` after hiding its source rows would lose model-visible history. Record
new outcomes in a stable `generation_mode=semantic|repaired|degraded` prefix in
`validation_failure_reason`, followed by bounded reason codes, and in structured
logs. Repaired summaries may retain the earlier failure reason. Readers must
accept existing rows unchanged; missing mode means legacy/unknown, not semantic
success. Audit consumers of this field before using it for quality metrics.

Emit mode, failure code, source/request size, logical call count, elapsed time,
first-event latency and available provider usage through existing observability.
Do not log source bodies or fabricate missing usage. Size measurements diagnose
input expansion; they do not replace provider token-budget authority.

#### Delivery and acceptance

Implement as one shared compaction change, without channel-specific alternatives:

1. Reuse ordinary history assembly in summary input and fix only shared history
   completeness defects needed by this path. Update the prompt, validation and
   fallback together. Keep public facade imports/test hooks valid.
2. Update shared generation, including media output allowance and terminal-status
   handling; extend snapshot/commit validation. Split runtime/archive/commit
   responsibilities into cohesive modules as needed to satisfy the 800-line gate.
3. Validate observable behavior in isolated local Docker, then review compatibility
   and the final diff before any separately authorized release.

Reuse affected compactor, history-reload, current-turn stale/recovery and media
tests. Cover complete ordinary-history reuse, unfamiliar actions/states,
same-call folding without merging distinct calls, tool replay, long instructions
with critical middle constraints, recalled/private content, consumed versus
pending inputs, failed/cancelled work, prior-marker/reference races, archive
failure, protocol completion, output truncation, media output allowance, and
legacy/degraded marker loading. Assertions must inspect actual generated requests,
returned history, persisted state or provider behavior, not source-code shape.

For semantic acceptance, use a small preannotated, scrubbed local corpus with
tool stubs: goal replacement/addition, bare continuation, multiple unfinished
goals, late constraints, Subagent feedback, failed actions, long current turns,
degraded recovery, and at least three successive compactions. Compare old/new
summaries and the first few continuation steps with the same model/settings and
retained suffix. Require zero lost permission/prohibition constraints, invented
completion, revived cancelled objectives or repeated external actions in these
cases; target at least 95% of annotated required goal/context facts. Report the
corpus, denominator and observed results, not a universal reliability claim.
Mocked responses prove lifecycle behavior, not this semantic quality target.

Candidate evaluation must not mutate live history. The existing compaction
dry-run path rolls its transaction back and does not insert a loadable marker or
flag source rows; production-derived evaluation must still use scrubbed isolated
local data. Existing degraded sessions are not repaired by this code change
alone. Any targeted regeneration must separately be
authorized, derive from original audit/archive sources, use the same ownership
and stale-commit checks, and never restart external work automatically.

#### 2026-09-20 implementation validation

Validation used the selected production model through isolated local containers;
no conversation action or tool schema was available to the evaluation calls.
The local backend ran the current checkout on port 3018 and reported healthy.
Two scrubbed, read-only production-derived histories were evaluated by comparing
the same model's execution-state reading of complete original history with its
reading of the generated summary plus retained original suffix:

- about 770,000 serialized characters: 108 replacement rows and 2 retained rows;
- about 584,000 serialized characters: 110 replacement rows and 7 retained rows.

The first iterations exposed real drift: exact pre-write safeguards were
generalized, a timeout expanded the re-probe scope, individual do-not-repeat
constraints disappeared, and one next-action list contradicted its own completed
or prohibited state. Each result was rejected rather than counted as success.
The final prompt makes continuity higher priority than brevity, preserves every
detail capable of changing execution, keeps explicit negative constraints
separate, does not soften required work into optional work, and checks that next
actions do not repeat completed or forbidden work. Final manual comparison found
the objective, permissions/prohibitions, completed-versus-open state, exact
safeguards, continuation order, uncertainty and no-repeat boundary materially
consistent for both histories. One candidate with zero replacement rows was
excluded as an invalid compaction sample.

This is evidence for the implemented failure cases, not a universal reliability
claim. It does not replace the broader annotated corpus and successive-compaction
evaluation above. Near-window full-history processing took minutes in these
checks; that latency is the explicit cost of complete source reuse and must be
tracked independently from semantic quality.

## Multimodal image history

History construction does not impose a separate image-count window or aggregate
image-byte budget. For a vision-capable model, every valid image in active
ordinary history is projected in original message order, whether it came from a
user attachment, compatible legacy input, or an assistant-visible tool result.
Per-file upload size, format, ownership and storage-access validation remain
input safety boundaries; they are not context-selection heuristics.

The default, neutral `add_media_to_ctx` capability adds images from any
valid path inside the current AgentDir rather than a product-folder allowlist.
It verifies actual image bytes and returns standard assistant-visible multimodal
tool-result blocks. The ordinary tool-result persistence and provider projection
then add those images to the current context and replay them while that tool
round remains active. Restoring an image referenced by a compacted summary is
one use of this general input capability, not a separate recall mechanism.
The tool itself never branches on provider, protocol or model capability. The
shared tool-result projection layer uses the selected model's input modalities
and `tool_result_multimodal_mode` to choose native or user-message projection;
unsupported image input follows that layer's standard unavailable-content
projection instead of a tool-specific fallback.
AgentDir containment, tenant ownership and per-file validation remain mandatory;
the tool cannot address an escaping or another Agent's path.

Provider-reported prompt usage and explicit provider overflow drive the shared
context-recovery loop. Compaction replaces eligible closed history as one unit,
so images in replaced rows naturally leave the active model request while audit
rows and permitted source files remain durable. Current/open turns retain their
exact image inputs. Do not add a second newest-N image selector, image-specific
summary store, or transport-specific history cap.

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
