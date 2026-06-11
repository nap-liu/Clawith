-- Add a per-user persistent HOME to the `svc` CLI tool (sandbox-injection model).
--
-- Context
-- -------
-- svc is already a type='cli' tool injected into the agent's aio-sandbox via an
-- identity-agnostic PATH wrapper + per-exec identity env (see
-- services/cli_tools/sandbox_inject.py). Its env is read through
-- CliToolConfig.model_validate, which lifts the legacy nested shape
-- (`config.runtime.env_inject`) into `cfg.env` WITHOUT a migration — so there is
-- deliberately NO "rewrite the whole config" step here (the earlier version of
-- this file did that and would have dropped GUANDATA_APP_TOKEN,
-- sandbox.memory_limit, and the runtime.* fields — a data-loss footgun).
--
-- The one thing the prod row is missing is YYBPC_CLI_HOME. Without it svc uses
-- HOME=<agent workspace>, which is SHARED by every user talking to that agent —
-- so svc's on-disk token cache (~/.yybpc-cli/token-cache) collides across users
-- and can serve one user's cached token to another (the stale-token 401 class
-- of bug). Pointing YYBPC_CLI_HOME at $state.dir (the per-(tenant,tool,user)
-- directory on the cli_state volume) makes svc's persistent state per-user,
-- matching the per-user identity it already runs under (YYBPC_CLI_USER_PHONE).
-- Setting env to $state.dir also flips on the state-dir provisioning in
-- agent_tools (needs_state = any value == '$state.dir').
--
-- This statement is ADDITIVE and IDEMPOTENT: it sets exactly one key via
-- jsonb_set and leaves binary / GUANDATA_APP_TOKEN / phone / runtime.* /
-- sandbox.* untouched. Safe to re-run.
--
-- Run manually at deploy time (prod changes need approval).
--
-- ── Deploy prerequisites (already true in prod, verify once) ───────────────
-- 1. aio-sandbox mounts the same named volumes as the backend, same paths:
--        config_cli_binaries:/data/cli_binaries:ro
--        config_cli_tool_state:/data/cli_state          (rw)
-- 2. aio-sandbox image = all-in-one-sandbox:1.9.3 (linux/amd64). 1.9.3 is
--    required for multi-line bash; the backend additionally delivers each exec
--    as a single-line base64 transport (bash <(echo <b64> | base64 -d)) so
--    comments / heredocs / multi-line survive verbatim.
--
-- Pre-check (inspect the current row first):
--   SELECT jsonb_pretty(config::jsonb) FROM tools WHERE name='svc' AND type='cli';
-- ─────────────────────────────────────────────────────────────────────────

UPDATE tools
SET config = jsonb_set(
    config::jsonb,
    '{runtime,env_inject,YYBPC_CLI_HOME}',
    '"$state.dir"'::jsonb,
    true  -- create_missing: add the key if absent
)
WHERE name = 'svc' AND type = 'cli';

-- Post-check (expect env_inject to now contain GUANDATA_APP_TOKEN +
-- YYBPC_CLI_USER_PHONE + YYBPC_CLI_HOME, everything else unchanged):
--   SELECT config::jsonb #> '{runtime,env_inject}' FROM tools WHERE name='svc';
