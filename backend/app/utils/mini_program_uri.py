"""Parser for the shared mini-program URI profile."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

MINI_PROGRAM_URI_SCHEME = "miniprogram"
_PREFIX = f"{MINI_PROGRAM_URI_SCHEME}://"
_ACTIONS = {"navigate-to"}
_PATH_SEGMENT = re.compile(r"^(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+$")
_QUERY = re.compile(r"^(?:[A-Za-z0-9._~!$&'()*+,;=:@/?-]|%[0-9A-Fa-f]{2})*$")


@dataclass(frozen=True)
class MiniProgramUri:
    action: str
    route: str


def parse_mini_program_uri(value: str) -> MiniProgramUri | None:
    """Parse a URI after validating its raw, non-normalized components."""

    raw = value.strip()
    if raw[: len(_PREFIX)].lower() != _PREFIX or "#" in raw:
        return None

    hierarchy_and_query = raw[len(_PREFIX) :]
    query_index = hierarchy_and_query.find("?")
    if query_index >= 0:
        hierarchy = hierarchy_and_query[:query_index]
        raw_query: str | None = hierarchy_and_query[query_index + 1 :]
    else:
        hierarchy = hierarchy_and_query
        raw_query = None

    path_index = hierarchy.find("/")
    if path_index <= 0:
        return None

    action = hierarchy[:path_index].lower()
    if action not in _ACTIONS:
        return None

    raw_path = hierarchy[path_index:]
    if raw_path == "/" or not raw_path.startswith("/"):
        return None
    if raw_query is not None and _QUERY.fullmatch(raw_query) is None:
        return None

    segments = raw_path[1:].split("/")
    if any(_PATH_SEGMENT.fullmatch(segment) is None for segment in segments):
        return None

    for segment in segments:
        try:
            decoded = unquote(segment, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        if (
            decoded in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or "\0" in decoded
        ):
            return None

    query_suffix = "" if raw_query is None else f"?{raw_query}"
    return MiniProgramUri(action=action, route=f"{raw_path}{query_suffix}")
