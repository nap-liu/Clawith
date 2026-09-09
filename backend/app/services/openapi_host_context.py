"""Non-secret browser bootstrap carried by the existing login ticket."""

from urllib.parse import urlsplit

from app.schemas.openapi_application import validate_origin
from app.services.openapi_applications import fail


def browser_origin(value):
    parsed = urlsplit(value)
    validate_origin(f"{parsed.scheme}://{parsed.netloc}")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}"


def host_context_bootstrap(body, public_base):
    if not body.host_context or not body.host_context.enabled:
        return None
    try:
        frame_origin = browser_origin(public_base)
    except (TypeError, ValueError):
        fail("host_context_unavailable", 503)
    return {"version": 1, "frame_origin": frame_origin,
            "embed_origin": browser_origin(body.embed_origin), "instance_ref": body.instance_ref}
