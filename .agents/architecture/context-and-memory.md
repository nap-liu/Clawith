# Context, compaction, and agent memory

## Budget authority

Context decisions use the selected model's configured capacity and ratio:

```text
effective prompt budget = floor(context_window * context_usage_ratio)
```

Provider-reported usage is the authority for successful model rounds. Do not
replace token accounting with character heuristics or a stale model-family
lookup. Primary/fallback dispatch must preserve a safe protected history suffix
for every model that may serve the turn.

The HTTP client may bound connect, write, and pool establishment, but does not
impose a generic read timeout over a valid long-running model response. Whole
tool-loop timeouts do not belong in channel adapters.

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

## Tool-result context

Persist the truthful original tool result. The bounded LLM view of fresh textual
tool output currently allows up to 32K characters per result and 64K in one
round. An over-limit view must say `TRUNCATED` and provide the exact path to the
complete stored content. Non-text/multimodal payloads do not pass through the
text character limiter.

Display/log sanitization must not poison the durable result later replayed to
the model. Exact file paths and stage-specific storage/parser errors remain
visible; do not collapse them into a false “not found” or canned success.

## Agent memory loading

Core memory is loaded without an arbitrary fixed-character truncation. Daily
memory loading is configured per Agent for 0–30 recent days; zero disables it.
Memory files must never contain credentials. Project Agents may map their
repository-owned memory layout through the runtime workspace abstraction, but
the context builder presents one normalized memory contract.

This product-level Agent memory is distinct from coding-agent instructions for
this repository. Repository engineering knowledge lives in `AGENTS.md`,
`.agents/`, code, and tests—not in any developer's personal assistant memory.
