"""Deterministic text projections shared by IM channel adapters."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_FENCED_CODE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_REFERENCE_DEFINITION_RE = re.compile(r"(?m)^\s{0,3}\[[^\]]+\]:\s+\S+(?:\s+.*)?$")
_AUTOLINK_RE = re.compile(r"<(https?://[^>]+)>", re.IGNORECASE)
_HORIZONTAL_RULE_RE = re.compile(r"(?m)^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"(?m)^\s*(?=[|:\-\s]*-{3})[|:\-\s]+$")
_LINE_PREFIX_RE = re.compile(r"(?m)^\s*(?:#{1,6}\s*|(?:>\s*)+|[-+*]\s+|\d+[.)]\s+)")
_TASK_MARK_RE = re.compile(r"(?m)^\s*\[[ xX]\]\s*")
_STRONG_MARK_RE = re.compile(r"(\*\*|__|~~)(?=\S)(.+?)(?<=\S)\1")
_EMPHASIS_MARK_RE = re.compile(r"(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)")
_WHITESPACE_RE = re.compile(r"\s+")
_NON_TEXT_SUMMARY = "非文本消息"


_HIDDEN_HTML_TAGS = {"head", "noscript", "script", "style", "template", "title"}


class _VisibleHTMLTextParser(HTMLParser):
    """Collect visible HTML text while ignoring non-visible element bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in _HIDDEN_HTML_TAGS:
            self._hidden_tags.append(normalized_tag)
        elif normalized_tag == "img" and not self._hidden_tags:
            alt = next((value for name, value in attrs if name.lower() == "alt"), None)
            if alt:
                self.parts.append(alt)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if self._hidden_tags and self._hidden_tags[-1] == normalized_tag:
            self._hidden_tags.pop()

    def handle_data(self, data: str) -> None:
        if not self._hidden_tags:
            self.parts.append(data)


def _protect_code(text: str) -> tuple[str, list[str], str]:
    protected: list[str] = []
    marker = "\ue000IMCODE"
    while marker in text:
        marker += "\ue000"

    def replace(match: re.Match[str]) -> str:
        protected.append(match.group(1))
        return f" {marker}{len(protected) - 1}\ue001 "

    text = _FENCED_CODE_RE.sub(replace, text)
    return _INLINE_CODE_RE.sub(replace, text), protected, marker


def _restore_code(text: str, protected: list[str], marker: str) -> str:
    for index, value in enumerate(protected):
        text = text.replace(f"{marker}{index}\ue001", value)
    return text


def _flatten_markdown_tables(text: str) -> str:
    lines = text.splitlines()
    table_rows: set[int] = set()
    for index, line in enumerate(lines):
        if not _TABLE_SEPARATOR_RE.fullmatch(line):
            continue
        if index > 0 and "|" in lines[index - 1]:
            table_rows.add(index - 1)
        next_index = index + 1
        while next_index < len(lines) and "|" in lines[next_index]:
            table_rows.add(next_index)
            next_index += 1
    return "\n".join(
        line.replace("|", " ") if index in table_rows else line
        for index, line in enumerate(lines)
    )


def _matching_delimiter(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 1
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _strip_inline_markdown_links(text: str) -> str:
    """Drop complete destinations while retaining link labels in one linear pass."""
    parts: list[str] = []
    index = 0
    while index < len(text):
        is_image = text.startswith("![", index)
        label_start = index + 1 if is_image else index
        if text[label_start : label_start + 1] != "[":
            parts.append(text[index])
            index += 1
            continue
        label_end = _matching_delimiter(text, label_start, "[", "]")
        destination_start = label_end + 1 if label_end is not None else -1
        if label_end is None:
            parts.append(text[index:])
            break
        if text[destination_start : destination_start + 1] == "[":
            reference_end = _matching_delimiter(text, destination_start, "[", "]")
            if reference_end is None:
                parts.append(text[index:])
                break
            parts.append(text[label_start + 1 : label_end])
            index = reference_end + 1
            continue
        if text[destination_start : destination_start + 1] != "(":
            parts.append(text[index:destination_start])
            index = destination_start
            continue
        destination_end = _matching_delimiter(text, destination_start, "(", ")")
        if destination_end is None:
            parts.append(text[index:])
            break
        parts.append(text[label_start + 1 : label_end])
        index = destination_end + 1
    return "".join(parts)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return f"{text[: max_chars - 1].rstrip()}…"


def extract_plain_text_summary(text: str, max_chars: int = 128) -> str:
    """Mechanically project message content to a deterministic one-line summary."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    projected, protected_code, code_marker = _protect_code(str(text or ""))
    projected = _AUTOLINK_RE.sub(r"\1", projected)
    projected = _strip_inline_markdown_links(projected)
    projected = _REFERENCE_DEFINITION_RE.sub("", projected)
    projected = _HORIZONTAL_RULE_RE.sub("", projected)
    projected = _LINE_PREFIX_RE.sub("", projected)
    projected = _TASK_MARK_RE.sub("", projected)
    projected = _STRONG_MARK_RE.sub(r"\2", projected)
    projected = _EMPHASIS_MARK_RE.sub(r"\2", projected)
    projected = _flatten_markdown_tables(projected)
    projected = _TABLE_SEPARATOR_RE.sub("", projected)

    parser = _VisibleHTMLTextParser()
    parser.feed(projected)
    parser.close()
    projected = _restore_code(" ".join(parser.parts), protected_code, code_marker)
    projected = _WHITESPACE_RE.sub(" ", projected).strip()

    return _truncate(projected, max_chars)


def project_nonempty_message_summary(text: str, max_chars: int = 128) -> str:
    """Return visible text, falling back to an exact mechanical source projection."""
    source = str(text or "")
    if not source.strip():
        raise ValueError("message text must not be empty")
    summary = extract_plain_text_summary(source, max_chars=max_chars)
    if summary:
        return summary
    return _truncate(_NON_TEXT_SUMMARY, max_chars)
