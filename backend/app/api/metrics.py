"""Prometheus scrape endpoint.

Exposes the process-wide default registry in the standard
`text/plain; version=0.0.4` format. Includes the `clawith_cli_tool_*`
metrics registered in `app.services.cli_tools.metrics` plus any other
counters / histograms that land on the default registry.

Access policy
-------------
This endpoint is **internal-only** and gated behind `platform_admin`.
A Prometheus server run by the operators would normally scrape it via a
service account with that role. Reasons not to leave it open:

* Per-tenant label values (`tenant_id`) are sensitive operational data.
* `process_*` metrics from prometheus_client's default collectors leak
  host details (memory, fd count, start time) that we don't want on
  the public internet.

Operators who want unauthenticated scrape access inside a private
network should put a reverse proxy in front of this route and strip
auth there — the application still refuses anonymous callers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.security import require_role

router = APIRouter(tags=["metrics"])


@router.get(
    "/metrics",
    # Bytes body + explicit media type — FastAPI's JSON serialization
    # would mangle the Prometheus text format.
    response_class=Response,
    include_in_schema=False,
    dependencies=[Depends(require_role("platform_admin"))],
)
async def prometheus_metrics() -> Response:
    """Return the default registry as Prometheus text exposition."""
    payload = generate_latest()
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)
