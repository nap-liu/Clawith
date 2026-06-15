#!/bin/bash
# Docker entrypoint: optionally run DB migrations, then start the app.

set -e

PROCESS_ROLE="${PROCESS_ROLE:-all}"
ALLOW_MIGRATION_FAILURE="${ALLOW_MIGRATION_FAILURE:-false}"
START_COMMAND="${START_COMMAND:-uvicorn app.main:app --host 0.0.0.0 --port 8000}"

role_contains() {
    case ",${PROCESS_ROLE}," in
        *,all,*|*,"$1",*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Permission fixing and privilege dropping ---
if [ "$(id -u)" = '0' ]; then
    echo "[entrypoint] Detected root user, checking permissions..."
    # Conditional chown (upstream #657): skip the slow recursive chown when the
    # data dir is already owned correctly — important once AGENT_DATA_DIR grows.
    TARGET_DIR="${AGENT_DATA_DIR:-/data/agents}"
    if [ -d "${TARGET_DIR}" ]; then
        CURRENT_OWNER=$(stat -c '%U:%G' "${TARGET_DIR}" 2>/dev/null || echo "")
        if [ "${CURRENT_OWNER}" != "clawith:clawith" ]; then
            echo "[entrypoint] Directory ${TARGET_DIR} owner is '${CURRENT_OWNER}', fixing permissions..."
            chown -R clawith:clawith "${TARGET_DIR}"
        else
            echo "[entrypoint] Directory ${TARGET_DIR} is already owned by clawith:clawith, skipping chown."
        fi
    fi
    # CLI-tool binaries volume — docker creates it as root on first mount.
    if [ -d /data/cli_binaries ]; then
        chown -R clawith:clawith /data/cli_binaries
    fi
    # CLI-tool persistent-HOME state. The subprocess backend runs as
    # clawith (single UID). Older installs ran a docker/bwrap sandbox as
    # `nobody` (uid 65534) and left directories here owned by nobody —
    # after the switch to subprocess-only those files become unreadable
    # to clawith. Reclaim any non-clawith files on startup; find+chown
    # is a no-op once converged.
    if [ -d /data/cli_state ]; then
        find /data/cli_state \! -user clawith -exec chown clawith:clawith {} +
        chmod 0755 /data/cli_state
    fi

    echo "[entrypoint] Dropping privileges to 'clawith' and re-executing..."
    exec gosu clawith /bin/bash "$0" "$@"
fi
# -------------------------------------------------------

if [ -z "${INSTANCE_ID:-}" ]; then
    SAFE_PROCESS_ROLE="${PROCESS_ROLE//,/-}"
    export INSTANCE_ID="${SAFE_PROCESS_ROLE}-$(hostname)"
fi
echo "[entrypoint] INSTANCE_ID=${INSTANCE_ID}"

# Schema bring-up (table create_all + idempotent column patches + alembic) runs
# only on the bootstrap role so non-bootstrap replicas never mutate the schema.
if role_contains "bootstrap"; then
    echo "[entrypoint] Step 1a: Creating/verifying database tables for PROCESS_ROLE=${PROCESS_ROLE}..."

    python << 'PYEOF'
import asyncio, sys

async def main():
    # Import all models to populate Base.metadata before create_all
    from app.database import Base, engine
    import app.models.user           # noqa
    import app.models.agent          # noqa
    import app.models.task           # noqa
    import app.models.llm            # noqa
    import app.models.tool           # noqa
    import app.models.audit          # noqa
    import app.models.skill          # noqa
    import app.models.channel_config # noqa
    import app.models.schedule       # noqa
    import app.models.plaza          # noqa
    import app.models.activity_log   # noqa
    import app.models.org            # noqa
    import app.models.system_settings # noqa
    import app.models.invitation_code # noqa
    import app.models.tenant         # noqa
    import app.models.participant     # noqa
    import app.models.chat_session   # noqa
    import app.models.trigger        # noqa
    import app.models.notification   # noqa
    import app.models.gateway_message # noqa
    import app.models.chat_compaction # noqa  # FK target of chat_messages.compacted_into; fresh DB create_all needs it registered
    import app.models.focus          # noqa  # v1.9.3 AgentFocusItem table; fresh DB create_all needs it registered

    # Create all tables that don't exist yet (safe to run on every startup)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[entrypoint] Tables created/verified")

    # Apply safe column patches for existing installs that may be missing columns.
    # All statements use IF NOT EXISTS so they are fully idempotent.
    patches = [
        # Quota fields added in v0.2
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_message_limit INTEGER DEFAULT 50",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_message_period VARCHAR(20) DEFAULT 'permanent'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_messages_used INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_period_start TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_max_agents INTEGER DEFAULT 2",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_agent_ttl_hours INTEGER DEFAULT 48",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_expired BOOLEAN DEFAULT FALSE",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_calls_today INTEGER DEFAULT 0",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS max_llm_calls_per_day INTEGER DEFAULT 100",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_calls_reset_at TIMESTAMPTZ",
        # agent_tools source tracking added later
        "ALTER TABLE agent_tools ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'system'",
        "ALTER TABLE agent_tools ADD COLUMN IF NOT EXISTS installed_by_agent_id UUID",
        # chat_sessions channel tracking
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS source_channel VARCHAR(20) NOT NULL DEFAULT 'web'",
        # Token reset tracking
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_daily_reset TIMESTAMPTZ",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_monthly_reset TIMESTAMPTZ",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS tokens_used_total INTEGER DEFAULT 0",
        # OpenClaw Agent support
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_type VARCHAR(20) NOT NULL DEFAULT 'native'",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128)",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS openclaw_last_seen TIMESTAMPTZ",
        # SSO fields
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sso_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sso_domain VARCHAR(255)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_tenants_sso_domain ON tenants(sso_domain) WHERE sso_domain IS NOT NULL",
        # LLM model pool — temperature / max_output_tokens columns
        "ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS temperature FLOAT",
        "ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS max_output_tokens INTEGER",
        "ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS request_timeout INTEGER",
        # Notification agent routing
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS agent_id UUID",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS sender_name VARCHAR(100)",
        "CREATE INDEX IF NOT EXISTS ix_notifications_agent_id ON notifications(agent_id)",
    ]

    from sqlalchemy import text
    async with engine.begin() as conn:
        for sql in patches:
            try:
                await conn.execute(text(sql))
            except Exception as e:
                print(f"[entrypoint] Patch skipped ({e})")

    await engine.dispose()
    print("[entrypoint] Column patches applied")

asyncio.run(main())
PYEOF

    echo "[entrypoint] Step 1b: Running alembic migrations for PROCESS_ROLE=${PROCESS_ROLE}..."
    set +e
    ALEMBIC_OUTPUT=$(alembic upgrade head 2>&1)
    ALEMBIC_EXIT=$?
    set -e

    if [ $ALEMBIC_EXIT -ne 0 ]; then
        echo ""
        echo "========================================================================"
        echo "[entrypoint] ERROR: Alembic migration FAILED (exit code $ALEMBIC_EXIT)"
        echo "========================================================================"
        echo ""
        echo "$ALEMBIC_OUTPUT"
        echo ""
        if [ "$ALLOW_MIGRATION_FAILURE" = "true" ]; then
            echo "[entrypoint] Continuing because ALLOW_MIGRATION_FAILURE=true"
        else
            exit $ALEMBIC_EXIT
        fi
    else
        echo "[entrypoint] Alembic migrations completed successfully."
    fi
else
    echo "[entrypoint] Step 1: Skipping alembic for PROCESS_ROLE=${PROCESS_ROLE}"
fi

echo "[entrypoint] Step 2: Starting uvicorn..."
exec /bin/bash -lc "$START_COMMAND"
