"""Feishu calendar tools with Agent-local time projection."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.services.agent_tools_feishu_auth import (
    _get_agent_calendar_id,
    _get_feishu_credentials,
)
from app.services.feishu_service import feishu_service
from app.services.recipient_resolver import RecipientResolutionError
from app.services.timezone_utils import (
    format_datetime_for_agent,
    get_agent_timezone,
    normalize_timezone_name,
    parse_datetime_for_agent,
)


# The agent_tools facade replaces this with the channel-scoped ContextVar used
# by Feishu message handling. Keeping a local default also makes this split
# module safe to exercise directly.
channel_feishu_sender_open_id: ContextVar[str | None] = ContextVar(
    "feishu_calendar_sender_open_id",
    default=None,
)


async def _resolve_feishu_open_id(
    agent_id: uuid.UUID,
    canonical_user_id: object,
) -> tuple[str, str]:
    """Facade-injected resolver placeholder for direct module safety."""
    raise RuntimeError("Feishu recipient resolver is not initialized")


def _parse_calendar_datetime(value: object, timezone_name: str) -> datetime:
    """Parse one calendar time into an absolute UTC instant."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d+", text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    parsed = parse_datetime_for_agent(text, timezone_name)
    if parsed is None:
        raise ValueError("empty datetime")
    return parsed


def _format_calendar_datetime(value: datetime, timezone_name: str) -> str:
    return format_datetime_for_agent(value, timezone_name) or "?"


def _format_provider_timestamp(value: object, timezone_name: str) -> str:
    try:
        instant = datetime.fromtimestamp(int(str(value)), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value or "?")
    return _format_calendar_datetime(instant, timezone_name)


async def _calendar_timezone(agent_id: uuid.UUID, arguments: dict) -> str:
    effective_timezone = await get_agent_timezone(agent_id)
    requested_timezone = str(arguments.get("timezone") or "").strip()
    if not requested_timezone:
        return normalize_timezone_name(effective_timezone)
    try:
        ZoneInfo(requested_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid IANA timezone: {requested_timezone}") from exc
    return requested_timezone


async def _calendar_credentials(agent_id: uuid.UUID) -> tuple[str, str] | str:
    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    token = await feishu_service.get_tenant_access_token(app_id, app_secret)
    calendar_id, calendar_error = await _get_agent_calendar_id(token)
    if not calendar_id:
        return calendar_error or "❌ Failed to retrieve agent's primary calendar ID."
    return token, calendar_id


async def _resolve_list_range(
    agent_id: uuid.UUID,
    arguments: dict,
) -> tuple[str, datetime, datetime] | str:
    timezone_name = await get_agent_timezone(agent_id)
    now = datetime.now(timezone.utc)
    try:
        start = (
            _parse_calendar_datetime(arguments["start_time"], timezone_name)
            if arguments.get("start_time")
            else now
        )
        end = (
            _parse_calendar_datetime(arguments["end_time"], timezone_name)
            if arguments.get("end_time")
            else now + timedelta(days=7)
        )
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        return f"❌ Invalid calendar time: {exc}. Use ISO 8601 or a Unix timestamp."
    if end <= start:
        return "❌ end_time must be later than start_time."
    return timezone_name, start, end


async def _freebusy_section(
    *,
    token: str,
    sender_open_id: str | None,
    start: datetime,
    end: datetime,
    timezone_name: str,
) -> str:
    if not sender_open_id:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://open.feishu.cn/open-apis/calendar/v4/freebusy/list",
                headers={"Authorization": f"Bearer {token}"},
                params={"user_id_type": "open_id"},
                json={
                    "time_min": start.isoformat(),
                    "time_max": end.isoformat(),
                    "user_id": sender_open_id,
                },
            )
        payload = response.json()
        if payload.get("code") != 0:
            return ""
        busy_slots = payload.get("data", {}).get("freebusy_list", [])
        if not busy_slots:
            return f"📌 **用户真实日历**：该时段全部空闲。（时区：{timezone_name}）"
        lines = [f"📌 **用户真实日历（忙碌时段，{timezone_name}）**："]
        for slot in sorted(busy_slots, key=lambda item: item.get("start_time", "")):
            try:
                slot_start = _parse_calendar_datetime(slot.get("start_time"), timezone_name)
                slot_end = _parse_calendar_datetime(slot.get("end_time"), timezone_name)
                rendered = (
                    f"{_format_calendar_datetime(slot_start, timezone_name)} → "
                    f"{_format_calendar_datetime(slot_end, timezone_name)}"
                )
            except (TypeError, ValueError, OSError, OverflowError):
                rendered = f"{slot.get('start_time')} → {slot.get('end_time')}"
            lines.append(f"  🔴 {rendered}")
        return "\n".join(lines)
    except Exception as exc:
        return f"⚠️ Freebusy 查询异常: {exc}"


async def _feishu_calendar_list(agent_id: uuid.UUID, arguments: dict) -> str:
    resolved_range = await _resolve_list_range(agent_id, arguments)
    if isinstance(resolved_range, str):
        return resolved_range
    timezone_name, start, end = resolved_range

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    sender_open_id = channel_feishu_sender_open_id.get(None)
    canonical_user_id = str(arguments.get("user_id") or "").strip()
    if canonical_user_id:
        try:
            _, sender_open_id = await _resolve_feishu_open_id(agent_id, canonical_user_id)
        except RecipientResolutionError as exc:
            return exc.as_json()

    freebusy = await _freebusy_section(
        token=token,
        sender_open_id=sender_open_id,
        start=start,
        end=end,
        timezone_name=timezone_name,
    )

    calendar_id, calendar_error = await _get_agent_calendar_id(token)
    if not calendar_id:
        return freebusy or calendar_error or "❌ Failed to retrieve agent's primary calendar ID."

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "start_time": str(int(start.timestamp())),
                "end_time": str(int(end.timestamp())),
            },
        )
    payload = response.json()
    if payload.get("code") != 0:
        return freebusy or f"❌ Calendar API error: {payload.get('msg')} (code {payload.get('code')})"

    items = payload.get("data", {}).get("items", [])
    max_results = max(1, min(int(arguments.get("max_results") or 20), 100))
    visible_items = items[:max_results]
    lines: list[str] = []
    if visible_items:
        lines.append(f"📅 Bot 日历共 {len(items)} 个日程（显示时区：{timezone_name}）：")
    for event in visible_items:
        summary = event.get("summary", "(no title)")
        start_text = _format_provider_timestamp(
            event.get("start_time", {}).get("timestamp"),
            timezone_name,
        )
        end_text = _format_provider_timestamp(
            event.get("end_time", {}).get("timestamp"),
            timezone_name,
        )
        location = event.get("location", {}).get("name", "")
        location_text = f" | 📍{location}" if location else ""
        lines.append(
            f"- **{summary}** | 🕐{start_text} → {end_text}{location_text}  "
            f"(ID: `{event.get('event_id', '')}`)"
        )
    if freebusy:
        lines.append(freebusy)
    return "\n".join(lines) if lines else f"📅 该时间段内没有日程。（时区：{timezone_name}）"


def _parse_event_times(arguments: dict, timezone_name: str) -> tuple[datetime, datetime] | str:
    try:
        start = _parse_calendar_datetime(arguments.get("start_time"), timezone_name)
        end = _parse_calendar_datetime(arguments.get("end_time"), timezone_name)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        return f"❌ Invalid calendar time: {exc}. Use ISO 8601."
    if end <= start:
        return "❌ end_time must be later than start_time."
    return start, end


async def _feishu_calendar_create(agent_id: uuid.UUID, arguments: dict) -> str:
    summary = str(arguments.get("summary") or "").strip()
    for field in ("summary", "start_time", "end_time"):
        if not str(arguments.get(field) or "").strip():
            return f"❌ Missing required argument '{field}'"

    try:
        timezone_name = await _calendar_timezone(agent_id, arguments)
    except ValueError as exc:
        return f"❌ {exc}"
    event_times = _parse_event_times(arguments, timezone_name)
    if isinstance(event_times, str):
        return event_times
    start, end = event_times

    attendee_open_ids: list[str] = []
    attendee_display: list[str] = []
    for canonical_user_id in list(arguments.get("attendee_user_ids") or [])[:20]:
        try:
            display_name, open_id = await _resolve_feishu_open_id(agent_id, canonical_user_id)
        except RecipientResolutionError as exc:
            return exc.as_json()
        if open_id not in attendee_open_ids:
            attendee_open_ids.append(open_id)
            attendee_display.append(display_name)
    sender_open_id = channel_feishu_sender_open_id.get(None)
    if sender_open_id and sender_open_id not in attendee_open_ids:
        attendee_open_ids.append(sender_open_id)

    credentials = await _calendar_credentials(agent_id)
    if isinstance(credentials, str):
        return credentials
    token, calendar_id = credentials
    body: dict = {
        "summary": summary,
        "start_time": {"timestamp": str(int(start.timestamp())), "timezone": timezone_name},
        "end_time": {"timestamp": str(int(end.timestamp())), "timezone": timezone_name},
    }
    if arguments.get("description"):
        body["description"] = arguments["description"]
    if arguments.get("location"):
        body["location"] = {"name": arguments["location"]}

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{calendar_id}/events",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    payload = response.json()
    if payload.get("code") != 0:
        return f"❌ Failed to create event: {payload.get('msg')} (code {payload.get('code')})"

    event_id = payload.get("data", {}).get("event", {}).get("event_id", "")
    if attendee_open_ids and event_id:
        async with httpx.AsyncClient(timeout=20) as client:
            for open_id in attendee_open_ids:
                await client.post(
                    f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
                    json={"attendees": [{"type": "user", "user_id": open_id}]},
                    headers={"Authorization": f"Bearer {token}"},
                    params={"user_id_type": "open_id"},
                )

    attendees = f"\n**参与人**: {', '.join(attendee_display)}" if attendee_display else ""
    invite_note = "\n（已向您发送日历邀请，请在飞书日历中确认）" if attendee_open_ids else ""
    return (
        "✅ 日历事件已创建！\n"
        f"**标题**: {summary}\n"
        f"**时间**: {_format_calendar_datetime(start, timezone_name)} → "
        f"{_format_calendar_datetime(end, timezone_name)}{attendees}\n"
        f"**Event ID**: `{event_id}`{invite_note}"
    )


async def _feishu_calendar_update(agent_id: uuid.UUID, arguments: dict) -> str:
    event_id = str(arguments.get("event_id") or "").strip()
    if not event_id:
        return "❌ 'event_id' is required."
    try:
        timezone_name = await _calendar_timezone(agent_id, arguments)
    except ValueError as exc:
        return f"❌ {exc}"

    patch: dict = {}
    for field in ("summary", "description"):
        if arguments.get(field):
            patch[field] = arguments[field]
    if arguments.get("location"):
        patch["location"] = {"name": arguments["location"]}
    rendered_times: list[str] = []
    for field in ("start_time", "end_time"):
        if not arguments.get(field):
            continue
        try:
            instant = _parse_calendar_datetime(arguments[field], timezone_name)
        except (TypeError, ValueError, OSError, OverflowError) as exc:
            return f"❌ Invalid {field}: {exc}. Use ISO 8601."
        patch[field] = {
            "timestamp": str(int(instant.timestamp())),
            "timezone": timezone_name,
        }
        rendered_times.append(f"{field}={_format_calendar_datetime(instant, timezone_name)}")
    if not patch:
        return "ℹ️ No fields to update."

    credentials = await _calendar_credentials(agent_id)
    if isinstance(credentials, str):
        return credentials
    token, calendar_id = credentials
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.patch(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            json=patch,
            headers={"Authorization": f"Bearer {token}"},
        )
    payload = response.json()
    if payload.get("code") != 0:
        return f"❌ Failed to update: {payload.get('msg')} (code {payload.get('code')})"
    details = f" {'; '.join(rendered_times)}." if rendered_times else ""
    return f"✅ Event `{event_id}` updated. Changed: {', '.join(patch.keys())}.{details}"


async def _feishu_calendar_delete(agent_id: uuid.UUID, arguments: dict) -> str:
    event_id = str(arguments.get("event_id") or "").strip()
    if not event_id:
        return "❌ 'event_id' is required."
    credentials = await _calendar_credentials(agent_id)
    if isinstance(credentials, str):
        return credentials
    token, calendar_id = credentials
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.delete(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    payload = response.json()
    if payload.get("code") != 0:
        return f"❌ Failed to delete: {payload.get('msg')} (code {payload.get('code')})"
    return f"✅ Event `{event_id}` deleted successfully."


__all__ = [
    "_feishu_calendar_list",
    "_feishu_calendar_create",
    "_feishu_calendar_update",
    "_feishu_calendar_delete",
]
