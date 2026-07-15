"""Pure rendering + truncation for the session-introspection tools.

No DB access here — handlers fetch, this module turns rows into the LLM-facing
string while enforcing the per-message and total-payload caps that keep huge
conversations from blowing up the introspecting agent's context window.
"""

from __future__ import annotations

from app.services.session_query import encode_cursor

PER_MSG_CHARS = 2000     # single-message body cap
TOTAL_CHARS = 14000      # whole-read payload cap (leaves room for system/format)
SNIPPET_RADIUS = 100     # chars of context around a search hit


def _truncate(text: str | None, limit: int = PER_MSG_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + " …[truncated]"


def _sender_label(msg, senders: dict) -> str:
    role = msg.role
    if role == "user":
        return senders.get("users", {}).get(msg.user_id, "user")
    if role == "assistant":
        pid = getattr(msg, "participant_id", None)
        if pid and pid in senders.get("participants", {}):
            return senders["participants"][pid]
        return senders.get("agents", {}).get(msg.agent_id, "assistant")
    return role


def _channel_kind(s) -> str:
    if s.is_group:
        return "群聊"
    if s.source_channel == "agent":
        return "A2A"
    if s.source_channel == "trigger":
        return "自省"
    return "对话"


def empty_list(kind: str = "会话") -> str:
    return f"没有找到符合条件的{kind}。"


def render_session_list(sessions, counts: dict, counterparts: dict, *, total: int, offset: int, limit: int) -> str:
    if not sessions:
        return empty_list("会话")
    head = f"共 {total} 个会话，显示第 {offset + 1}–{offset + len(sessions)} 个："
    lines = [head]
    for i, s in enumerate(sessions, start=offset + 1):
        cid = str(s.id)
        title = s.group_name or s.title or "(无标题)"
        n = counts.get(cid, 0)
        last = s.last_message_at.isoformat() if s.last_message_at else "—"
        cp = counterparts.get(s.id, "?")
        lines.append(
            f"{i}. [{cid}] {title} · {_channel_kind(s)} · 通道={s.source_channel}"
            f" · 对端={cp} · {n} 条 · 最后={last}"
        )
    if offset + len(sessions) < total:
        lines.append(f"… 还有更多，用 offset={offset + limit} 继续。")
    return "\n".join(lines)


def render_messages(messages, senders: dict, *, more_available: bool) -> str:
    """Render a page chronologically, keeping the most-recent messages that fit
    under TOTAL_CHARS and pointing at a ``before`` cursor for older ones."""
    if not messages:
        return "该会话暂无可显示的消息。"

    # Keep the most recent messages that fit (walk newest->oldest), then show asc.
    kept = []
    total = 0
    for m in reversed(messages):
        cost = len(_truncate(m.content)) + 80  # rough per-line overhead
        if kept and total + cost > TOTAL_CHARS:
            break
        kept.append(m)
        total += cost
    kept.reverse()
    dropped_older = len(kept) < len(messages)

    blocks = []
    for m in kept:
        ts = m.created_at.isoformat() if m.created_at else "—"
        blocks.append(f"[{ts}] {_sender_label(m, senders)} ({m.role}):\n{_truncate(m.content)}")
    body = "\n\n".join(blocks)
    if more_available or dropped_older:
        body += f"\n\n… 还有更早的消息，用 before={encode_cursor(kept[0])} 继续读。"
    return body


def _snippet(content: str | None, keyword: str) -> str:
    if not content:
        return ""
    idx = content.lower().find(keyword.lower())
    if idx < 0:
        return _truncate(content, 200).replace("\n", " ")
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(content), idx + len(keyword) + SNIPPET_RADIUS)
    return content[start:end].replace("\n", " ")


def render_search_hits(hits, titles: dict, *, keyword: str, channels: dict | None = None) -> str:
    if not hits:
        return f"没有找到包含「{keyword}」的消息。"
    lines = [f"命中 {len(hits)} 条包含「{keyword}」的消息（最多 {len(hits)} 条；如过多请缩小关键词）："]
    channels = channels or {}
    for m in hits:
        ts = m.created_at.isoformat() if m.created_at else "—"
        title = titles.get(str(m.conversation_id), "?")
        channel = channels.get(str(m.conversation_id), "?")
        lines.append(
            f"- [session {m.conversation_id}] {title} · 通道={channel} · {ts}"
            f"\n  …{_snippet(m.content, keyword)}…"
        )
    return "\n".join(lines)
