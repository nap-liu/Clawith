-- Remove the finish protocol tool rows (code no longer defines/uses `finish`).
-- Run BEFORE deploying the code that removes the protocol (order per release rule:
-- backfill first, then code). Idempotent; safe to re-run.
--
-- Old code tolerates missing rows (finish was re-injected from _ALWAYS_INCLUDE_CORE
-- at runtime), so running this against a stack still on the old code is harmless.

BEGIN;

-- 1) Per-agent assignments
DELETE FROM agent_tools
WHERE tool_id IN (SELECT id FROM tools WHERE name = 'finish' AND category = 'system');

-- 2) Tenant-level tool config rows, if any (key pattern: tool_config:<tool_name>)
DELETE FROM tenant_settings
WHERE key = 'tool_config:finish';

-- 3) The tool row itself
DELETE FROM tools WHERE name = 'finish' AND category = 'system';

COMMIT;
