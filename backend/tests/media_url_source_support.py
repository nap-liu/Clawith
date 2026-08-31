"""Shared values and helpers for managed media URL tests."""

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide"
SESSION_ID = "session-managed"


def _staging_dir(agent_root, session_id=SESSION_ID):
    return agent_root / ".tool_results" / session_id / ".media"


def _fail_temp_directory(**_kwargs):
    raise OSError("temp full")


async def _async_addresses(value):
    return value
