"""Shared media and temporary-workspace materialization limits."""

TOOL_MATERIALIZE_MAX_FILE_BYTES = 10 * 1024 * 1024
TOOL_MATERIALIZE_MAX_TOTAL_BYTES = 100 * 1024 * 1024
MEDIA_TOOL_MAX_FILE_BYTES = 100 * 1024 * 1024


def _media_materialization_size_error(
    media_size: int,
    cover_size: int | None = None,
) -> str | None:
    """Return the stable preflight error before selective materialization."""
    if media_size > MEDIA_TOOL_MAX_FILE_BYTES:
        return "MEDIA_TOO_LARGE"
    if cover_size is not None and cover_size > TOOL_MATERIALIZE_MAX_FILE_BYTES:
        return "VIDEO_COVER_TOO_LARGE"
    if media_size + (cover_size or 0) > TOOL_MATERIALIZE_MAX_TOTAL_BYTES:
        return "MEDIA_BUNDLE_TOO_LARGE"
    return None


__all__ = [
    "MEDIA_TOOL_MAX_FILE_BYTES",
    "TOOL_MATERIALIZE_MAX_FILE_BYTES",
    "TOOL_MATERIALIZE_MAX_TOTAL_BYTES",
    "_media_materialization_size_error",
]
