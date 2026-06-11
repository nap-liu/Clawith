-- Fix the `svc` CLI tool description: stop inducing wrong agent behavior.
--
-- svc is surfaced to the LLM as a standalone function whose single parameter is
-- a `command` STRING (the full bash command line, starting with `svc`). The DB
-- description still carried two inductions left over from the old array-based
-- args_template model:
--
--   1. Array-form examples — ["help"], ["report","list"],
--      ["report","query","--report-id", ...] — which contradict the actual
--      `command` string parameter and nudge the model to pass an array.
--   2. A self-contradicting example: it demonstrated
--      `--report-id yk_manager_overview_1` while the SAME description says
--      "【yk_*】开头的报表全都不维护了 不要使用". Showing a forbidden value as the
--      worked example is exactly the kind of negative-example induction we avoid;
--      replaced with a neutral <报表ID> placeholder.
--
-- Only the first sentence and the example line change; the business rules
-- (report-first, yk_* deprecated, familiarize-with-reports-first) are preserved
-- verbatim. svc is a single global tool (tenant_id NULL), so this one row is
-- shared by every agent — no per-agent fix needed.
--
-- Touches only the `description` column (config untouched). Idempotent.

UPDATE tools
SET description =
'查询报表、主档、门店、供应链、财务、会员等数据。command 参数传入完整命令行（以 svc 开头）。
【重要】: 所有的数据查询优先使用 report 子命令进行查询，这个是唯一数据分析的来源!! 只有 report 中没有涵盖的数据才允许使用其他的命令查询数据 ，使用数据查询之前一定要先熟悉一下所有的 report 中的报表内容，【yk_*】开头的报表全都不维护了 不要使用这些报表。示例: svc help 查看帮助; svc report list 查看所有的数据报表; svc report query --report-id <报表ID> 查看指定报表数据'
WHERE name = 'svc' AND type = 'cli';

-- Post-check:
--   SELECT description FROM tools WHERE name='svc' AND type='cli';
