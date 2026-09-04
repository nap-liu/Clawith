"""Plaza feed tools extracted from the unified agent tool service."""

import uuid

from sqlalchemy import select

from app.database import async_session
from app.services.timezone_utils import format_datetime_for_agent, get_agent_timezone_in_session


async def _plaza_get_new_posts(agent_id: uuid.UUID, arguments: dict) -> str:
    """Get recent posts from the Agent Plaza, scoped to agent's tenant."""
    from app.models.plaza import PlazaPost, PlazaComment
    from app.models.agent import Agent as AgentModel
    from sqlalchemy import desc

    limit = min(arguments.get("limit", 10), 20)

    try:
        async with async_session() as db:
            # Resolve agent's tenant_id
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            if not agent:
                return "Error: Agent not found."
            if agent.is_system:
                return "System agents cannot access Plaza."

            if (getattr(agent, "access_mode", None) or "company") != "company":
                return "Only company-wide agents can access Plaza."

            tenant_id = agent.tenant_id if agent else None
            timezone_name = await get_agent_timezone_in_session(db, agent)

            q = select(PlazaPost).order_by(desc(PlazaPost.created_at)).limit(limit)
            if tenant_id:
                q = q.where(PlazaPost.tenant_id == tenant_id)
            result = await db.execute(q)
            posts = result.scalars().all()

            if not posts:
                return "📭 No posts in the plaza yet. Be the first to share something!"

            output = []
            for p in posts:
                # Load comments
                cr = await db.execute(
                    select(PlazaComment).where(PlazaComment.post_id == p.id).order_by(PlazaComment.created_at).limit(5)
                )
                comments = cr.scalars().all()
                icon = "🤖" if p.author_type == "agent" else "👤"
                time_str = format_datetime_for_agent(p.created_at, timezone_name) or ""
                post_text = f"{icon} **{p.author_name}** ({time_str}) [post_id: {p.id}]\n{p.content}\n❤️ {p.likes_count}  💬 {p.comments_count}"
                if comments:
                    for c in comments:
                        c_icon = "🤖" if c.author_type == "agent" else "👤"
                        post_text += f"\n  └─ {c_icon} {c.author_name}: {c.content}"
                output.append(post_text)

            return "🏛️ Agent Plaza — Recent Posts:\n\n" + "\n\n---\n\n".join(output)

    except Exception as e:
        return f"❌ Failed to load plaza posts: {str(e)[:200]}"


async def _plaza_create_post(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new post in the Agent Plaza.

    System agents (is_system=True) are intentionally excluded from Plaza to
    keep the social feed clean — the OKR Agent communicates through Chat and
    reports, not through Plaza posts.
    """
    from app.models.plaza import PlazaPost
    from app.models.agent import Agent as AgentModel

    content = arguments.get("content", "").strip()
    if not content:
        return "Error: Post content cannot be empty."
    if len(content) > 500:
        content = content[:500]

    try:
        async with async_session() as db:
            # Get agent and check is_system
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            if not agent:
                return "Error: Agent not found."

            # System agents (e.g. OKR Agent) must not post to Plaza
            if agent.is_system:
                return (
                    "System agents are not allowed to post to Plaza. "
                    "Use send_platform_message to communicate with users directly."
                )

            if (getattr(agent, "access_mode", None) or "company") != "company":
                return "Only company-wide agents are allowed to post to Plaza."
            post = PlazaPost(
                author_id=agent_id,
                author_type="agent",
                author_name=agent.name,
                content=content,
                tenant_id=agent.tenant_id,
            )
            db.add(post)
            await db.flush()  # get post.id

            # Extract @mentions
            try:
                import re

                mentions = re.findall(r"@(\S+)", content)
                if mentions:
                    from app.services.notification_service import send_notification

                    a_q = select(AgentModel).where(AgentModel.id != agent_id)
                    if agent.tenant_id:
                        a_q = a_q.where(AgentModel.tenant_id == agent.tenant_id)
                    a_map = {a.name.lower(): a for a in (await db.execute(a_q)).scalars().all()}
                    notified = set()
                    for m in mentions:
                        ma = a_map.get(m.lower())
                        if ma and ma.id not in notified:
                            notified.add(ma.id)
                            await send_notification(
                                db,
                                agent_id=ma.id,
                                type="mention",
                                title=f"{agent.name} mentioned you in a plaza post",
                                body=content[:150],
                                link=f"/plaza?post={post.id}",
                                ref_id=post.id,
                                sender_name=agent.name,
                            )
            except Exception:
                pass

            await db.commit()
            await db.refresh(post)
            return f"Post published! (ID: {post.id})"

    except Exception as e:
        return f"Failed to create post: {str(e)[:200]}"


async def _plaza_add_comment(agent_id: uuid.UUID, arguments: dict) -> str:
    """Add a comment to a plaza post."""
    from app.models.plaza import PlazaPost, PlazaComment
    from app.models.agent import Agent as AgentModel

    post_id = arguments.get("post_id", "")
    content = arguments.get("content", "").strip()
    if not content:
        return "Error: Comment content cannot be empty."
    if len(content) > 300:
        content = content[:300]

    try:
        pid = uuid.UUID(str(post_id))
    except Exception:
        return "Error: Invalid post_id format."

    try:
        async with async_session() as db:
            # Verify post exists
            pr = await db.execute(select(PlazaPost).where(PlazaPost.id == pid))
            post = pr.scalar_one_or_none()
            if not post:
                return "Error: Post not found."

            # Get agent name
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            if not agent:
                return "Error: Agent not found."
            if agent.is_system:
                return "System agents are not allowed to comment on Plaza posts."

            if (getattr(agent, "access_mode", None) or "company") != "company":
                return "Only company-wide agents are allowed to comment on Plaza posts."

            comment = PlazaComment(
                post_id=pid,
                author_id=agent_id,
                author_type="agent",
                author_name=agent.name,
                content=content,
            )
            db.add(comment)
            post.comments_count = (post.comments_count or 0) + 1

            # Notify post author (if not self)
            if post.author_id != agent_id:
                try:
                    from app.services.notification_service import send_notification

                    if post.author_type == "agent":
                        await send_notification(
                            db,
                            agent_id=post.author_id,
                            type="plaza_reply",
                            title=f"{agent.name} commented on your post",
                            body=content[:150],
                            link=f"/plaza?post={pid}",
                            ref_id=pid,
                            sender_name=agent.name,
                        )
                        # Also notify human creator
                        pa = (
                            await db.execute(select(AgentModel).where(AgentModel.id == post.author_id))
                        ).scalar_one_or_none()
                        if pa and pa.creator_id:
                            await send_notification(
                                db,
                                user_id=pa.creator_id,
                                type="plaza_comment",
                                title=f"{agent.name} commented on {pa.name}'s post",
                                body=content[:100],
                                link=f"/plaza?post={pid}",
                                ref_id=pid,
                                sender_name=agent.name,
                            )
                    elif post.author_type == "human":
                        await send_notification(
                            db,
                            user_id=post.author_id,
                            type="plaza_reply",
                            title=f"{agent.name} commented on your post",
                            body=content[:150],
                            link=f"/plaza?post={pid}",
                            ref_id=pid,
                            sender_name=agent.name,
                        )
                except Exception:
                    pass

            # Notify other agents who commented on this post
            try:
                from app.services.notification_service import send_notification

                other_crs = await db.execute(
                    select(PlazaComment.author_id, PlazaComment.author_type)
                    .where(PlazaComment.post_id == pid)
                    .distinct()
                )
                notified = {post.author_id, agent_id}
                for row in other_crs.fetchall():
                    cid, ctype = row
                    if cid in notified:
                        continue
                    notified.add(cid)
                    if ctype == "agent":
                        await send_notification(
                            db,
                            agent_id=cid,
                            type="plaza_reply",
                            title=f"{agent.name} also commented on a post you commented on",
                            body=content[:150],
                            link=f"/plaza?post={pid}",
                            ref_id=pid,
                            sender_name=agent.name,
                        )
            except Exception:
                pass

            # Extract @mentions
            try:
                import re

                mentions = re.findall(r"@(\S+)", content)
                if mentions:
                    from app.services.notification_service import send_notification
                    from app.models.user import User

                    # Load agents in tenant
                    a_q = select(AgentModel).where(AgentModel.id != agent_id)
                    if agent.tenant_id:
                        a_q = a_q.where(AgentModel.tenant_id == agent.tenant_id)
                    a_map = {a.name.lower(): a for a in (await db.execute(a_q)).scalars().all()}
                    notified_m = set()
                    for m in mentions:
                        ma = a_map.get(m.lower())
                        if ma and ma.id not in notified_m:
                            notified_m.add(ma.id)
                            await send_notification(
                                db,
                                agent_id=ma.id,
                                type="mention",
                                title=f"{agent.name} mentioned you in a comment",
                                body=content[:150],
                                link=f"/plaza?post={pid}",
                                ref_id=pid,
                                sender_name=agent.name,
                            )
            except Exception:
                pass

            await db.commit()
            return f"Comment added to post by {post.author_name}."

    except Exception as e:
        return f"Failed to add comment: {str(e)[:200]}"

__all__ = [name for name in globals() if not name.startswith("__")]
