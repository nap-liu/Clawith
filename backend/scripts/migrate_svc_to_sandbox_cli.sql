-- Migrate the `svc` CLI tool to the sandbox-injection model (v5 config).
--
-- After this migration svc is no longer a subprocess CLI tool nor an LLM
-- function: it is injected as a bash function into the agent's aio-sandbox
-- shell. Config collapses to {binary, env}; parameters_schema is retired
-- (cli tools left the LLM function list — see get_agent_tools_for_llm);
-- description becomes the agent-facing command usage docs that get appended
-- to execute_code_aio's description.
--
-- Run manually at deploy time (follows the "prod changes need approval" rule).
--
-- ── Deploy checklist (prod) ──────────────────────────────────────────────
-- 1. compose: aio-sandbox service mounts the SAME named volumes the backend
--    uses, at the same in-container paths:
--        config_cli_binaries:/data/cli_binaries:ro
--        config_cli_tool_state:/data/cli_state          (rw)
--    (volumes already exist; project prefix is `config` in prod.)
-- 2. aio-sandbox image = ghcr.io/agent-infra/sandbox:1.9.3 (linux/amd64).
--    1.9.3 is required: its shell-exec layer joins multi-line bash with ';'
--    (1.0.0.152 returned ErrorObservation for any '\n' command, which broke
--    multi-line agent bash and the svc injection block). svc is a linux-x64
--    Node SEA binary, so the amd64 image is mandatory. Build/push backend +
--    all services together (lockstep).
-- 3. run this SQL; verify:
--        SELECT config, parameters_schema FROM tools WHERE name='svc';
--    expect config = {"binary": {...}, "env": {...}}, parameters_schema = {}.
-- 4. verify get_agent_tools_for_llm output: NO `svc` function in the list;
--    execute_code_aio description contains the svc usage docs.
-- 5. smoke in chat: agent runs `svc --version` and
--    `svc report list --agent | head` inside execute_code_aio (bash).
-- 6. R-A: sandbox egress must reach api.yeyecha.com (SSO agentLogin is
--    IP-whitelisted). Same host egress as backend → expected OK; verify once.
--
-- Pre-check (inspect current row before running):
--   SELECT config, parameters_schema, description FROM tools WHERE name='svc';
-- ─────────────────────────────────────────────────────────────────────────

UPDATE tools SET
  config = jsonb_build_object(
    -- preserve the system-written binary metadata (sha256/size/…) as-is
    'binary', (config::jsonb)->'binary',
    'env', jsonb_build_object(
      'YYBPC_CLI_USER_PHONE', '$user.phone',
      'YYBPC_CLI_HOME', '$state.dir'
    )
  ),
  parameters_schema = '{}'::json,
  description = '黄鹤楼主档数据查询 CLI。用法: svc <子命令> [参数],输出 JSON,可接管道(| jq | head)。建议加 --agent 压缩输出。【重要】所有数据查询优先使用 report 子命令(svc report list 查看全部报表; svc report query --report-id <id> 查询数据),report 是唯一数据分析来源,只有 report 未涵盖的数据才允许使用其他子命令。yk_* 开头的报表已不维护,不要使用。'
WHERE name = 'svc' AND type = 'cli';

-- Post-check:
--   SELECT config, parameters_schema FROM tools WHERE name='svc';
