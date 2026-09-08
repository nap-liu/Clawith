#!/bin/bash
# Docker entrypoint: optionally run DB migrations, then start the app.

set -e

PROCESS_ROLE="${PROCESS_ROLE:-all}"
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

# The standalone command and Alembic CLI share the same bootstrap boundary.
if role_contains "bootstrap"; then
    echo "[entrypoint] Preparing database schema..."
    python -m app.scripts.bootstrap_db
else
    echo "[entrypoint] Skipping schema setup for PROCESS_ROLE=${PROCESS_ROLE}"
fi

echo "[entrypoint] Step 2: Starting uvicorn..."
exec /bin/bash -lc "$START_COMMAND"
