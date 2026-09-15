"""Shared guard for text that can become visible to product users."""

import re
from app.utils.internal_login_redaction import INCOMPLETE_CREDENTIAL, redact_internal_login

# Keep the legacy brand token out of new user-facing source text as well as at
# runtime. Infrastructure identifiers remain outside this presentation guard.
_BANNED_PRODUCT_KEYWORD = "cla" "with"
_OPENING_WRAPPERS = "[\uff08\u3010({\u300c\u300e\u3008\u300a"
_CLOSING_WRAPPERS = "]\uff09\u3011)}\u300d\u300f\u3009\u300b"
_WRAPPED_BANNED_RE = re.compile(
    rf"[{re.escape(_OPENING_WRAPPERS)}][ \t]*"
    rf"{re.escape(_BANNED_PRODUCT_KEYWORD)}[ \t]*"
    rf"[{re.escape(_CLOSING_WRAPPERS)}][ \t]*",
    re.IGNORECASE,
)
_BANNED_RE = re.compile(
    rf"{re.escape(_BANNED_PRODUCT_KEYWORD)}[ \t]*",
    re.IGNORECASE,
)
_SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"[ \t]+(?=[,.;:!?\uff0c\u3002\uff1b\uff1a\uff01\uff1f])")


def sanitize_user_visible_text(text: str) -> str:
    """Remove the banned product keyword without leaving empty wrappers.

    Matching is case-insensitive and applies even when the token is embedded in
    another word. Text that does not contain the token is returned unchanged.
    """
    text = redact_internal_login(text)
    if not _BANNED_RE.search(text):
        return text

    cleaned = _WRAPPED_BANNED_RE.sub("", text)
    cleaned = _BANNED_RE.sub("", cleaned)
    cleaned = _SPACE_BEFORE_PUNCTUATION_RE.sub("", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.split("\n"))


class UserOutputStreamSanitizer:
    """Redact a forbidden token even when a model splits it across chunks."""

    _GUARD_CHARS = 32

    def __init__(self) -> None:
        self._tail = ""

    def feed(self, chunk: str) -> str:
        combined = self._tail + str(chunk or "")
        pending = INCOMPLETE_CREDENTIAL.search(combined)
        if pending:
            prefix = combined[:pending.start()]
            if len(prefix) <= self._GUARD_CHARS:
                self._tail = combined
                return ""
            self._tail = prefix[-self._GUARD_CHARS:] + combined[pending.start():]
            return sanitize_user_visible_text(prefix[:-self._GUARD_CHARS])
        cleaned = sanitize_user_visible_text(combined)
        if len(cleaned) <= self._GUARD_CHARS:
            self._tail = cleaned
            return ""
        emitted = cleaned[:-self._GUARD_CHARS]
        self._tail = cleaned[-self._GUARD_CHARS:]
        return emitted

    def flush(self) -> str:
        emitted = sanitize_user_visible_text(self._tail)
        self._tail = ""
        return emitted
