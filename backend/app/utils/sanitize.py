"""Sanitize sensitive fields from tool call arguments before sending to clients."""

import re
from copy import deepcopy
from urllib.parse import urlparse, urlunparse

# Data-URL for inline images — redacted to prevent base64 payload pollution
# in audit logs, WebSocket broadcasts, and session history.
_DATA_IMAGE_URI_RE = re.compile(
    r"^data:image/(jpeg|png|webp|gif);base64,",
    re.IGNORECASE,
)


def _redact_if_base64_image(value):
    """If `value` is a data:image/*;base64,… URI, return a placeholder.
    Otherwise return the value unchanged. Safe on non-strings.
    """
    if not isinstance(value, str):
        return value
    if not _DATA_IMAGE_URI_RE.match(value):
        return value
    # Approximate decoded size without actually decoding.
    header, _, payload = value.partition(",")
    size_kb = max(1, (len(payload) * 3 // 4) // 1024)
    return f"[base64 image, {size_kb} KB]"


# Field names whose values should be completely hidden (replaced with "******")
SENSITIVE_FIELD_NAMES = {
    "password", "secret", "token", "api_key", "apikey", "api_secret",
    "access_token", "refresh_token", "private_key", "secret_key",
    "authorization", "credentials", "auth",
    # Connection/credential strings — hide entirely, not partially
    "connection_string", "database_url", "db_url", "dsn", "uri",
    "connection_uri", "jdbc_url", "mongo_uri", "redis_url",
}


def sanitize_tool_args(args: dict | None) -> dict | None:
    """Return a sanitized copy of tool call arguments.

    - Fields matching SENSITIVE_FIELD_NAMES are replaced with "******"
    - Values that look like connection URIs are also replaced with "******"
    - Original dict is NOT modified (returns a deep copy)
    """
    if not args:
        return args

    sanitized = deepcopy(args)

    for key in list(sanitized.keys()):
        key_lower = key.lower()

        # Fully mask sensitive fields by name
        if key_lower in SENSITIVE_FIELD_NAMES:
            sanitized[key] = "******"
            continue

        # Fully mask values that look like connection URIs regardless of field name
        if isinstance(sanitized[key], str) and _looks_like_connection_uri(sanitized[key]):
            sanitized[key] = "******"

        # Redact base64 image data URIs (strings and list items)
        val = sanitized[key]
        if isinstance(val, str):
            sanitized[key] = _redact_if_base64_image(val)
        elif isinstance(val, list):
            sanitized[key] = [_redact_if_base64_image(item) for item in val]

    # Special case: hide content when writing to secrets.md
    path_val = sanitized.get("path", "") or ""
    if _is_secrets_file_path(path_val):
        if "content" in sanitized:
            sanitized["content"] = "******"

    return sanitized


def _is_secrets_file_path(path: str) -> bool:
    """Check if a path references secrets.md."""
    normalized = path.strip("/")
    return normalized == "secrets.md" or normalized.endswith("/secrets.md")


def _mask_uri_password(uri: str) -> str:
    """Mask the password portion of a connection URI.

    mysql://user:secret123@host:3306/db -> mysql://user:******@host:3306/db
    """
    try:
        parsed = urlparse(uri)
        if parsed.password:
            # Reconstruct with masked password
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            if parsed.username:
                netloc = f"{parsed.username}:******@{netloc}"
            return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        pass

    # Fallback: regex-based masking for non-standard URIs
    return re.sub(r'(://[^:]+:)[^@]+(@)', r'\1******\2', uri)


def _looks_like_connection_uri(value: str) -> bool:
    """Check if a string value looks like a database connection URI."""
    prefixes = ("mysql://", "postgresql://", "postgres://", "sqlite://",
                "mongodb://", "redis://", "mssql://", "oracle://",
                "mysql+", "postgresql+", "postgres+")
    return any(value.lower().startswith(p) for p in prefixes)
