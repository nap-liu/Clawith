"""Published-page tools extracted from the unified agent tool service."""

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from app.database import async_session
from app.models.audit import AuditLog
from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.storage import get_storage_backend


async def _resolve_public_base_url() -> str:
    """Resolve the platform's public base URL for building shareable links.

    Reads, in priority order, the PUBLIC_BASE_URL env var then the value an admin
    saved in the web UI (system_settings.platform.public_base_url). Returns an
    empty string when nothing public is configured — callers then fall back to a
    relative path rather than emitting a localhost link.
    """
    try:
        from app.services.platform_service import platform_service

        async with async_session() as db:
            base = (await platform_service.get_public_base_url(db=db) or "").rstrip("/")
        # get_public_base_url returns http://localhost:8000 as its last-resort
        # default; treat that as "not publicly configured" so we don't hand out
        # links that only resolve on the server itself.
        if base and base != "http://localhost:8000":
            return base
    except Exception:
        pass
    return (os.environ.get("PUBLIC_BASE_URL", "") or "").rstrip("/")


async def _publish_page(agent_id: uuid.UUID, user_id: uuid.UUID, ws: Path, arguments: dict) -> str:
    """Publish an HTML file with optional access control."""
    import secrets
    import re

    path = arguments.get("path", "")
    access_mode = arguments.get("access_mode", "authenticated")
    effective_access_mode = access_mode
    allowed_user_ids = arguments.get("allowed_user_ids", []) if access_mode == "restricted" else []
    if not path:
        return "Missing required argument 'path'"
    if access_mode not in {"public", "authenticated", "restricted"}:
        return "Invalid access_mode; use public, authenticated, or restricted"

    # Validate file extension
    if not path.lower().endswith((".html", ".htm")):
        return "Only .html and .htm files can be published"

    # Resolve via storage backend (supports local FS and S3)
    storage = get_storage_backend()
    storage_key = current_agent_runtime_workspace(agent_id).storage_key(path)
    if not await storage.exists(storage_key) or not await storage.is_file(storage_key):
        return f"File not found: {path}"

    # Extract title from HTML
    try:
        content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip()[:200] if title_match else Path(path).stem
    except Exception:
        title = Path(path).stem

    # Stable URL per source file. Re-publishing the SAME file must return the
    # SAME short_id: the /p/<id> route serves the file live, so one stable link
    # already reflects every edit. Minting a fresh id on each publish (the old
    # behavior) scattered views across dozens of equivalent links and left users
    # asking "which link is current?" — e.g. one report file had 25 distinct
    # short_ids. So reuse an existing page for this (agent_id, source_path);
    # only mint a new id when the file was never published before.
    from app.models.published_page import PublishedPage
    from app.models.user import User

    reused = False
    page_id: uuid.UUID | None = None
    publication_actor_label = f"unknown user (user_id: {user_id})"
    publication_time: datetime | None = None
    try:
        async with async_session() as db:
            actor = await db.get(User, user_id)
            if actor is not None:
                publication_actor_label = f"{actor.display_name} (user_id: {actor.id})"
            publication_time = datetime.now(timezone.utc)
            existing = (
                await db.execute(
                    select(PublishedPage)
                    .where(
                        PublishedPage.agent_id == agent_id,
                        PublishedPage.source_path == path,
                    )
                    .order_by(PublishedPage.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                short_id = existing.short_id
                page_id = existing.id
                reused = True
                existing.last_published_by_user_id = user_id
                existing.last_published_at = publication_time
                if title and existing.title != title:
                    existing.title = title  # refresh title; same link
                if "access_mode" in arguments:
                    from app.services.published_page_access import can_manage_page

                    if actor is None or not await can_manage_page(db, existing, actor):
                        return "Permission denied: only the page publisher or Agent creator can change page access"
                    if existing.tenant_id is None:
                        existing.tenant_id = actor.tenant_id
                    existing.access_mode = access_mode
                    await _replace_page_allowed_users(db, existing, allowed_user_ids, user_id)
                effective_access_mode = existing.access_mode
                await db.commit()
            else:
                # New file → mint a short_id and resolve tenant_id for the row.
                tenant_id = None
                try:
                    from app.models.agent import Agent as _AgModel

                    _r = await db.execute(select(_AgModel.tenant_id).where(_AgModel.id == agent_id))
                    tenant_id = _r.scalar_one_or_none()
                    if tenant_id is None:
                        from app.models.user import User as _UserModel

                        tenant_id = await db.scalar(select(_UserModel.tenant_id).where(_UserModel.id == user_id))
                except Exception:
                    tenant_id = None
                short_id = secrets.token_urlsafe(6)[:8]  # 8-char URL-safe string
                db.add(
                    PublishedPage(
                        short_id=short_id,
                        agent_id=agent_id,
                        user_id=user_id,
                        last_published_by_user_id=user_id,
                        last_published_at=publication_time,
                        tenant_id=tenant_id,
                        source_path=path,
                        title=title,
                        access_mode=access_mode,
                    )
                )
                await db.flush()
                created = await db.scalar(select(PublishedPage).where(PublishedPage.short_id == short_id))
                if created:
                    page_id = created.id
                    await _replace_page_allowed_users(db, created, allowed_user_ids, user_id)
                await db.commit()
    except Exception as e:
        return f"Failed to publish: {e}"

    # Build the public URL via the platform service so it picks up the domain
    # an admin configured in the web UI (system_settings.platform.public_base_url),
    # not just the PUBLIC_BASE_URL env var. Priority: env → DB → localhost fallback.
    public_base = await _resolve_public_base_url()
    if not public_base:
        # Nothing public is configured; fall back to a relative path that still
        # works inside the same deployment, plus a hint for the admin.
        url = f"/p/{short_id}"
        url_note = (
            "\n\n> Note: no public base URL is configured on this server. "
            "The link above is a relative path — prepend your server's domain "
            "to get the full URL. An admin can set it in the company settings "
            "(or PUBLIC_BASE_URL in .env) so links come out fully-qualified."
        )
    else:
        url = f"{public_base}/p/{short_id}"
        url_note = ""
    management_path = f"/published-pages?page={page_id}" if page_id else "/published-pages"
    management_url = f"{public_base}{management_path}" if public_base else management_path

    headline = (
        "Updated in place — the page already had a published link, so the SAME URL "
        "now serves the latest content (no new link is created)."
        if reused
        else "Published successfully!"
    )
    return (
        f"{headline}\n\n"
        f"Page URL: {url}\n"
        f"Management URL: {management_url}\n"
        f"Title: {title}\n\n"
        f"Published by: {publication_actor_label}\n"
        f"Published at: {publication_time.isoformat() if publication_time else 'not recorded'}\n\n"
        f"Access: {effective_access_mode}.\n"
        f"Platform watermark: enabled automatically ({'anonymous visitor ID and access time' if effective_access_mode == 'public' else 'signed-in user identity'}).\n"
        "Automatic SSO: off by default; append ?auto_login=1 only when explicitly requested, "
        "and optionally append &sso=<provider_type>."
        f"{url_note}"
    )


async def _list_published_pages(agent_id: uuid.UUID) -> str:
    """List all published pages for this agent."""
    from app.models.published_page import PublishedPage, PublishedPageAccess
    from app.models.user import User

    public_base = await _resolve_public_base_url()

    try:
        async with async_session() as db:
            pending_requests = (
                select(
                    PublishedPageAccess.page_id.label("page_id"),
                    func.count(PublishedPageAccess.id).label("pending_count"),
                )
                .where(
                    PublishedPageAccess.status == "pending",
                    PublishedPageAccess.requested_at.is_not(None),
                )
                .group_by(PublishedPageAccess.page_id)
                .subquery()
            )
            result = await db.execute(
                select(PublishedPage, func.coalesce(pending_requests.c.pending_count, 0))
                .outerjoin(pending_requests, pending_requests.c.page_id == PublishedPage.id)
                .where(PublishedPage.agent_id == agent_id)
                .order_by(PublishedPage.created_at.desc())
            )
            pages = result.all()

            actor_ids = {
                actor_id
                for published_page, _pending_count in pages
                for actor_id in (
                    published_page.user_id,
                    published_page.last_published_by_user_id,
                )
                if actor_id is not None
            }
            actor_rows = []
            if actor_ids:
                actor_rows = (await db.scalars(select(User).where(User.id.in_(actor_ids)))).all()
            actors = {user.id: f"{user.display_name} (user_id: {user.id})" for user in actor_rows}

        if not pages:
            return "No published pages yet."

        management_path = f"/published-pages?agent_id={agent_id}"
        management_url = f"{public_base}{management_path}" if public_base else management_path
        lines = [f"Published pages ({len(pages)} total):", f"Management URL: {management_url}\n"]
        for p, pending_count in pages:
            url = f"{public_base}/p/{p.short_id}" if public_base else f"/p/{p.short_id}"
            page_management_path = f"/published-pages?page={p.id}"
            page_management_url = f"{public_base}{page_management_path}" if public_base else page_management_path
            lines.append(f"- {p.title or 'Untitled'}")
            lines.append(f"  URL: {url}")
            lines.append(f"  Management: {page_management_url}")
            lines.append(f"  Source: {p.source_path}")
            lines.append(f"  Created by: {actors.get(p.user_id, 'unknown user')}")
            lines.append(f"  Created at: {p.created_at.isoformat() if p.created_at else 'not recorded'}")
            lines.append(
                f"  Last published by: {actors.get(p.last_published_by_user_id, 'historical data not recorded')}"
            )
            lines.append(
                "  Last published at: "
                f"{p.last_published_at.isoformat() if p.last_published_at else 'historical data not recorded'}"
            )
            lines.append(f"  Views: {p.view_count}")
            lines.append(f"  Access: {p.access_mode}")
            lines.append(f"  Pending access requests: {int(pending_count or 0)}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to list pages: {e}"


async def _list_page_access_requests(agent_id: uuid.UUID, user_id: uuid.UUID, arguments: dict) -> str:
    """List user-initiated access requests for a page managed by the caller."""
    from app.models.published_page import PublishedPage, PublishedPageAccess
    from app.models.user import Identity, User
    from app.services.published_page_access import can_manage_page

    short_id = str(arguments.get("short_id") or "").strip()
    if not short_id:
        return "Missing required argument 'short_id'"
    status = str(arguments.get("status") or "all").strip().lower()
    if status not in {"all", "pending", "approved", "rejected"}:
        return "Invalid status; use all, pending, approved, or rejected"
    try:
        page_number = max(1, int(arguments.get("page", 1)))
        page_size = min(50, max(1, int(arguments.get("page_size", 20))))
    except (TypeError, ValueError):
        return "Invalid pagination; page and page_size must be integers"

    try:
        async with async_session() as db:
            published_page = await db.scalar(
                select(PublishedPage).where(
                    PublishedPage.agent_id == agent_id,
                    PublishedPage.short_id == short_id,
                )
            )
            if not published_page:
                return "Published page not found for this Agent"
            actor = await db.get(User, user_id)
            if actor is None or not await can_manage_page(db, published_page, actor):
                return "Permission denied: only the page publisher or Agent creator can view access requests"

            conditions = [
                PublishedPageAccess.page_id == published_page.id,
                PublishedPageAccess.requested_at.is_not(None),
            ]
            if status != "all":
                conditions.append(PublishedPageAccess.status == status)
            total = int(await db.scalar(select(func.count()).select_from(PublishedPageAccess).where(*conditions)) or 0)
            pending_count = int(
                await db.scalar(
                    select(func.count())
                    .select_from(PublishedPageAccess)
                    .where(
                        PublishedPageAccess.page_id == published_page.id,
                        PublishedPageAccess.requested_at.is_not(None),
                        PublishedPageAccess.status == "pending",
                    )
                )
                or 0
            )
            rows = (
                await db.execute(
                    select(User, Identity, PublishedPageAccess)
                    .join(PublishedPageAccess, PublishedPageAccess.user_id == User.id)
                    .outerjoin(Identity, Identity.id == User.identity_id)
                    .where(*conditions)
                    .order_by(PublishedPageAccess.requested_at.desc(), PublishedPageAccess.id.desc())
                    .offset((page_number - 1) * page_size)
                    .limit(page_size)
                )
            ).all()

        public_base = await _resolve_public_base_url()
        management_path = f"/published-pages?page={published_page.id}"
        management_url = f"{public_base}{management_path}" if public_base else management_path
        lines = [
            f"Access requests for {published_page.title or published_page.source_path} (/p/{short_id})",
            f"Filter: {status}; page {page_number}; showing {len(rows)} of {total}",
            f"Pending requests: {pending_count}",
            f"Management URL: {management_url}",
        ]
        if not rows:
            lines.append("No matching user-initiated access requests.")
            return "\n".join(lines)
        for requester, identity, access_request in rows:
            requester_email = identity.email if identity and identity.email else "no email"
            lines.extend(
                [
                    "",
                    f"- {requester.display_name} ({requester_email})",
                    f"  User ID: {requester.id}",
                    f"  Status: {access_request.status}",
                    f"  Requested at: {access_request.requested_at.isoformat()}",
                    f"  Resolved at: {access_request.resolved_at.isoformat() if access_request.resolved_at else 'not resolved'}",
                ]
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Failed to list page access requests: {exc}"


async def _replace_page_allowed_users(db, page, raw_user_ids, resolved_by: uuid.UUID) -> None:
    from datetime import datetime, timezone
    from app.models.published_page import PublishedPageAccess
    from app.models.user import User

    try:
        user_ids = {uuid.UUID(str(value)) for value in (raw_user_ids or [])}
    except ValueError as exc:
        raise ValueError("allowed_user_ids contains an invalid user ID") from exc
    if user_ids:
        valid_ids = set(
            (
                await db.scalars(
                    select(User.id).where(
                        User.id.in_(user_ids), User.tenant_id == page.tenant_id, User.is_active.is_(True)
                    )
                )
            ).all()
        )
        if valid_ids != user_ids:
            raise ValueError("All allowed users must be active members of the page's company")
    rows = (await db.scalars(select(PublishedPageAccess).where(PublishedPageAccess.page_id == page.id))).all()
    by_user = {row.user_id: row for row in rows}
    for row in rows:
        if row.status == "approved" and row.user_id not in user_ids:
            await db.delete(row)
    now = datetime.now(timezone.utc)
    for viewer_id in user_ids:
        row = by_user.get(viewer_id)
        if row:
            row.status, row.resolved_at, row.resolved_by = "approved", now, resolved_by
        else:
            db.add(
                PublishedPageAccess(
                    page_id=page.id, user_id=viewer_id, status="approved", resolved_at=now, resolved_by=resolved_by
                )
            )


async def _search_page_viewers(agent_id: uuid.UUID, user_id: uuid.UUID, arguments: dict) -> str:
    from sqlalchemy import or_
    from app.models.agent import Agent
    from app.models.user import Identity, User

    query = str(arguments.get("query") or "").strip()
    if not query:
        return "Missing required argument 'query'"
    async with async_session() as db:
        from app.core.permissions import check_agent_access

        actor = await db.get(User, user_id)
        if actor is None:
            return "Permission denied: user not found"
        try:
            _agent, access = await check_agent_access(db, actor, agent_id)
        except Exception:
            return "Permission denied: Agent manage access required"
        if access != "manage":
            return "Permission denied: Agent manage access required"
        tenant_id = await db.scalar(select(Agent.tenant_id).where(Agent.id == agent_id))
        rows = (
            await db.execute(
                select(User, Identity)
                .outerjoin(Identity, Identity.id == User.identity_id)
                .where(
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                    or_(User.display_name.ilike(f"%{query}%"), Identity.email.ilike(f"%{query}%")),
                )
                .order_by(User.display_name)
                .limit(20)
            )
        ).all()
    if not rows:
        return "No matching users."
    return "\n".join(
        f"- {user.display_name} ({identity.email if identity else 'no email'}): {user.id}" for user, identity in rows
    )


async def _update_published_page_access(agent_id: uuid.UUID, user_id: uuid.UUID, arguments: dict) -> str:
    from app.core.permissions import is_platform_admin_user
    from app.models.published_page import PublishedPage
    from app.models.user import User
    from app.services.published_page_access import can_manage_page

    short_id = str(arguments.get("short_id") or "")
    access_mode = arguments.get("access_mode")
    if access_mode not in {"public", "authenticated", "restricted"}:
        return "Invalid access_mode; use public, authenticated, or restricted"
    try:
        async with async_session() as db:
            actor = await db.get(User, user_id)
            if actor is None:
                return "Permission denied: user not found"
            page_query = select(PublishedPage).where(PublishedPage.short_id == short_id)
            if not (is_platform_admin_user(actor) or actor.role == "org_admin"):
                page_query = page_query.where(PublishedPage.agent_id == agent_id)
            page = await db.scalar(page_query.with_for_update())
            if not page:
                return "Published page not found or not manageable from this conversation"
            if not await can_manage_page(db, page, actor):
                return "Permission denied: page management access required"
            if page.tenant_id is None:
                page.tenant_id = actor.tenant_id
            page.access_mode = access_mode
            allowed_user_ids = arguments.get("allowed_user_ids", []) if access_mode == "restricted" else []
            await _replace_page_allowed_users(db, page, allowed_user_ids, user_id)
            db.add(
                AuditLog(
                    user_id=user_id,
                    agent_id=page.agent_id,
                    action="published_page_access_updated",
                    details={
                        "page_id": str(page.id),
                        "access_mode": access_mode,
                        "allowed_user_ids": [str(value) for value in allowed_user_ids],
                        "source": "agent_tool",
                    },
                )
            )
            await db.commit()
        return f"Updated /p/{short_id} access to {access_mode}."
    except Exception as exc:
        return f"Failed to update page access: {exc}"

__all__ = [name for name in globals() if not name.startswith("__")]
