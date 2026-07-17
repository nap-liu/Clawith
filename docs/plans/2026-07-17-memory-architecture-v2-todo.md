# Persistent Memory Architecture V2 — Immutable Iteration TODO

> Created: 2026-07-17
> Branch: `feat/memory-architecture-v2`
> Baseline: `company/main@3803d7a60fa7d706a72ad273b07306d077c75822`
> Status: implementation, neutral audit remediation, and local verification complete

## Governance

- The seven numbered workstreams below are the frozen scope approved by the user.
- Their count, direction, and acceptance boundary must not be removed, merged, split, or redefined during this iteration.
- Only checkbox status and evidence may change. Any scope change requires explicit user approval and an appended decision record; it must not rewrite the original items.
- `memory/memory.md` is the only Core Memory, `memory/MEMORY_INDEX.md` is an ordinary structure guide, and `memory/<YYYY-MM-DD>/memory.md` is the only Daily Memory layout.
- Static system-prefix bytes must remain stable. Memory-file changes are the explicit dynamic exception and must stay in the current-turn tail without rewriting earlier messages.

## Frozen workstreams

1. [x] **Canonical structure and templates** — Define one Core Memory file, one ordinary fixed structure guide, and date-scoped Daily Memory files without new identity concepts or database schema.

2. [x] **Deterministic layered loading** — Always load full Core Memory and the structure guide, then load at most the two most recent existing non-empty Daily Memory files using the Agent timezone; retain older files for explicit lookup.

3. [x] **Static model guidance** — Put scenario-independent memory semantics, lookup rules, credential boundaries, and strong Daily Memory write guidance in the static system prompt without embedding a concrete date.

4. [x] **Tool-loop persistence and prefix stability** — Attach one immutable memory/context snapshot to the current user turn and preserve it through every tool and output-resume round by append-only message growth.

5. [x] **Creation and one-time existing-Agent migration** — Seed templates for new workspaces and provide an idempotent one-time backfill for existing workspaces without protecting or continuously recreating `MEMORY_INDEX.md`.

6. [x] **Tool-contract closure** — Keep runtime DB seeder definitions and fallback definitions aligned, verify the LLM-visible file-tool descriptions, and preserve ordinary read/write/edit/delete behavior for all memory files.

7. [x] **Docker and 3008 regression evidence** — Run focused, database-backed and relevant full regression in the worktree Docker environment; verify local 3008 health and the real context/tool schema path without starting competing IM subscriptions.

## Verification evidence

- Focused memory/context/tool-loop/workspace/backfill suite: 44 passed in Docker; the four neutral-audit boundary fixes plus S3 conditional-create contract passed 21 focused tests.
- Confirmation/recovery compatibility passed 29 associated tests, and the DB-backed turn-recovery suite passed 21 tests in its own isolated Docker process.
- Full backend suite in isolated `clawith_test` Postgres using the production-dependency Docker image: 1,479 passed, 28 skipped, with the same two unrelated `test_pages_csp.py` 404 failures previously reproduced unchanged on clean `company/main`.
- Runtime DB source of truth: `seed_builtin_tools()` updated both file-tool descriptions, and `get_agent_tools_for_llm()` returned both `write_file` and `edit_file` with the Daily Memory path and maintenance guidance.
- Prefix contract: confirmation continuation appends a context-only tail without rewriting historical User messages; primary/fallback share the exact same frozen context tuple; max-output resume and later tool rounds preserve every prior dispatched message byte.
- Ordinary file contract: `memory/MEMORY_INDEX.md` passed normal workspace deletion, while local and S3 `require_absent` writes are atomic and the one-time backfill retains concurrent Agent/admin writes and the unverified legacy source.
- Production-version startup smoke: worktree code started successfully on backend image `v1.10.3-3803d7a` with `PROCESS_ROLE=api`, returned health version `1.10.3`, and started no connector/IM role. Existing local 3008 health also remained `1.10.3` after cleanup.

## Appended decision record

### 2026-07-17 — Neutral audit P1 remediation

- A read-only neutral Subagent audit found no P0 and four P1 boundary defects after the original implementation: confirmation continuation could wrap an old User message, provider failover rebuilt the turn context, backfill creation was non-atomic, and max-output recovery removed already-dispatched messages.
- The user approved remediation without changing the seven frozen workstreams or the memory-file architecture.
- The accepted direction is minimal and source-level: wrap only a tail User or append context-only User; build one frozen context per failover turn; use storage-native atomic conditional create; and retain the full recovery transcript as an append-only prefix.
