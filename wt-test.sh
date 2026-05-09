#!/bin/bash
# Worktree test runner.
# Mounts the worktree backend into a fresh clawith-backend-test container,
# overrides the image's uvicorn entrypoint, and runs pytest against the
# shared dev postgres/redis at clawith_default network.
#
# Usage:  ./wt-test.sh tests/test_X.py [tests/test_Y.py ...] [-- additional pytest flags]
# Example: ./wt-test.sh tests/test_agent_context.py -v --tb=short

set -euo pipefail

WORKTREE_BACKEND="/Users/liuxi/projects/yybpc/clawith/.worktrees/mcp-prompt-foundation/backend"
AGENT_DATA="/Users/liuxi/projects/yybpc/clawith/backend/agent_data"

if [ $# -eq 0 ]; then
  echo "Usage: $0 tests/test_X.py [tests/test_Y.py ...] [-- pytest flags]" >&2
  exit 2
fi

docker run --rm --network clawith_default \
  --entrypoint python \
  -v "$WORKTREE_BACKEND":/app \
  -v "$AGENT_DATA":/data/agents \
  -e DATABASE_URL=postgresql+asyncpg://clawith:clawith@postgres:5432/clawith \
  -e REDIS_URL=redis://redis:6379/0 \
  -e AGENT_DATA_DIR=/data/agents \
  -e AGENT_TEMPLATE_DIR=/app/agent_template \
  -e SECRET_KEY=test \
  -e JWT_SECRET_KEY=test \
  -e CORS_ORIGINS='["*"]' \
  ${MCP_USE_LEGACY_COLLECTOR:+-e MCP_USE_LEGACY_COLLECTOR="$MCP_USE_LEGACY_COLLECTOR"} \
  -w /app \
  clawith-backend-test \
  -m pytest "$@"
