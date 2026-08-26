"""Autonomy boundary enforcement service.

Implements the three-level autonomy system:
  L1 — Auto-execute, notify creator
  L2 — Notify creator, auto-execute
  L3 — Require explicit approval before execution
"""

import json
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.services.user_output import sanitize_user_visible_text


def _safe_user_label(value: object, fallback: str) -> str:
    return sanitize_user_visible_text(str(value or "")).strip() or fallback


async def _prepare_creator_feishu_message(
    *,
    db: AsyncSession,
    agent: Agent,
    creator: User,
    receive_id: str,
    receive_id_type: str,
    text_content: str,
    artifact_role: str,
) -> dict:
    """Create a pending creator message in the caller's transaction."""
    from app.services.channel_session import find_or_create_channel_session
    from app.services.chat_history import persist_assistant_reply_row
    from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta

    text_content = sanitize_user_visible_text(text_content)
    external_conv_id = f"feishu_p2p_{receive_id}"
    session = await find_or_create_channel_session(
        db=db,
        agent_id=agent.id,
        user_id=creator.id,
        external_conv_id=external_conv_id,
        source_channel="feishu",
        first_message_title=text_content[:30],
    )
    message_id = await persist_assistant_reply_row(
        db,
        agent_id=agent.id,
        user_id=creator.id,
        conversation_id=str(session.id),
        content=text_content,
        message_meta=attach_delivery_to_meta(
            {"artifact_role": artifact_role},
            IMDeliveryResult.pending("feishu"),
        ),
    )
    return {
        "message_id": str(message_id),
        "agent_id": str(agent.id),
        "conversation_id": str(session.id),
        "external_conv_id": external_conv_id,
        "receive_id": receive_id,
        "receive_id_type": receive_id_type,
        "text": text_content,
    }


async def deliver_prepared_creator_notifications(items: list[dict]) -> None:
    """Deliver creator messages only after their owning transaction commits."""
    from app.services.im_delivery import deliver_persisted_message
    from app.services.turn_runtime import TurnRuntime

    for item in items:
        result = await deliver_persisted_message(
            message_id=item["message_id"],
            agent_id=uuid.UUID(item["agent_id"]),
            runtime=TurnRuntime(
                session_found=True,
                source_channel="feishu",
                conversation_id=item["conversation_id"],
                external_conv_id=item["external_conv_id"],
                is_group=False,
            ),
            message=item["text"],
            origin_actor_ref=item["receive_id"],
            origin_actor_ref_type=item["receive_id_type"],
        )
        if not result.ok:
            raise RuntimeError(result.error or "feishu_creator_delivery_failed")


class AutonomyService:
    """Enforce autonomy boundaries for agent operations."""

    async def check_and_enforce(
        self,
        db: AsyncSession,
        agent: Agent,
        action_type: str,
        details: dict,
        *,
        forced_level: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Check if an action is allowed under the agent's autonomy policy.

        Returns:
            {
                "allowed": True/False,
                "level": "L1"/"L2"/"L3",
                "approval_id": uuid (if L3),
                "message": str,
            }
        """
        policy = agent.autonomy_policy or {}
        level = forced_level or policy.get(action_type, "L2")  # Default to L2
        details = dict(details)
        if idempotency_key:
            details["idempotency_key"] = idempotency_key

        # Log the action regardless of level
        audit = AuditLog(
            agent_id=agent.id,
            action=f"autonomy_check:{action_type}",
            details={"level": level, **details},
        )
        db.add(audit)

        if level == "L1":
            # Auto-execute, just log
            logger.info(f"L1: Auto-executing {action_type} for agent {agent.name}")
            return {
                "allowed": True,
                "level": "L1",
                "message": "Auto-executed",
            }

        elif level == "L2":
            # Auto-execute but notify creator
            logger.info(f"L2: Executing {action_type} for agent {agent.name} with notification")
            pending_notifications = await self._notify_creator(
                db, agent, action_type, details
            )
            return {
                "allowed": True,
                "level": "L2",
                "message": "Executed and creator notified",
                "_pending_im_notifications": pending_notifications,
            }

        elif level == "L3":
            if idempotency_key:
                # Serialize lookup + creation so a replayed tool call has one
                # durable approval request across workers.
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:approval_key, 0))"),
                    {"approval_key": idempotency_key},
                )
                existing = await db.scalar(
                    select(ApprovalRequest).where(
                        ApprovalRequest.agent_id == agent.id,
                        ApprovalRequest.action_type == action_type,
                        ApprovalRequest.details["idempotency_key"].as_string() == idempotency_key,
                    )
                )
                if existing:
                    return {
                        "allowed": False,
                        "level": "L3",
                        "approval_id": str(existing.id),
                        "approval_status": existing.status,
                        "message": "Approval request already exists",
                    }

            # Create approval request and block
            approval = ApprovalRequest(
                agent_id=agent.id,
                action_type=action_type,
                details=details,
            )
            db.add(approval)
            await db.flush()

            logger.info(f"L3: Approval required for {action_type} by agent {agent.name}")
            pending_notifications = await self._request_approval(
                db, agent, approval
            )

            return {
                "allowed": False,
                "level": "L3",
                "approval_id": str(approval.id),
                "message": "Approval requested from creator",
                "_pending_im_notifications": pending_notifications,
            }

        return {"allowed": False, "level": "unknown", "message": "Unknown autonomy level"}

    async def resolve_approval(
        self, db: AsyncSession, approval_id: uuid.UUID, user: User, action: str
    ) -> ApprovalRequest:
        """Approve or reject a pending approval request."""
        if action not in {"approve", "reject"}:
            raise ValueError("Action must be 'approve' or 'reject'")

        result = await db.execute(
            select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
        )
        approval = result.scalar_one_or_none()
        if not approval:
            raise ValueError("Approval not found")

        if approval.status != "pending":
            raise ValueError("Approval already resolved")

        # Permission check: only agent creator or platform admin can resolve
        agent_result = await db.execute(select(Agent).where(Agent.id == approval.agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            raise ValueError("Approval agent not found")
        if agent.creator_id != user.id and user.role != "platform_admin":
            raise ValueError("Only the agent creator or platform admin can resolve approvals")

        resolved_status = "approved" if action == "approve" else "rejected"
        resolved_at = datetime.now(timezone.utc)

        # Compare-and-swap is the single execution gate across workers.  Commit the
        # terminal decision before any external side effect so a racing request (or a
        # retry after execution starts) cannot execute the action a second time.
        claim = await db.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.status == "pending",
            )
            .values(status=resolved_status, resolved_at=resolved_at, resolved_by=user.id)
            .returning(ApprovalRequest.id)
        )
        if claim.scalar_one_or_none() is None:
            await db.rollback()
            raise ValueError("Approval already resolved")

        # Log
        db.add(AuditLog(
            user_id=user.id,
            agent_id=approval.agent_id,
            action=f"approval_{resolved_status}",
            details={"approval_id": str(approval.id), "action_type": approval.action_type},
        ))
        await db.commit()
        approval = await db.get(ApprovalRequest, approval_id)

        # Post-processing: execute the approved action
        execution_result = None
        if resolved_status == "approved" and approval.details:
            execution_result = await self._execute_approved_action(
                approval.agent_id,
                approval.action_type,
                approval.details,
                user.id,
            )
            logger.info(f"Post-approval execution for {approval.action_type}: {execution_result}")

        # Web notification to agent creator about the result
        if agent:
            from app.services.notification_service import send_notification
            status_label = resolved_status
            body_text = sanitize_user_visible_text(
                json.dumps(approval.details, ensure_ascii=False)[:200]
            )
            if execution_result:
                body_text = sanitize_user_visible_text(f"Result: {execution_result}")
            safe_agent_name = _safe_user_label(agent.name, "Agent")
            safe_action_type = _safe_user_label(approval.action_type, "action")
            await send_notification(
                db,
                user_id=agent.creator_id,
                type="approval_resolved",
                title=f"{safe_agent_name}: {safe_action_type} — {status_label}",
                body=body_text,
                link=f"/agents/{agent.id}#approvals",
                ref_id=approval.id,
            )

            # Also notify the user who requested the action (if different from creator)
            requested_by = approval.details.get("requested_by") if approval.details else None
            if requested_by:
                try:
                    requester_id = uuid.UUID(requested_by)
                    if requester_id != agent.creator_id:
                        await send_notification(
                            db,
                            user_id=requester_id,
                            type="approval_resolved",
                            title=f"{safe_agent_name}: {safe_action_type} — {status_label}",
                            body=body_text,
                            link=f"/agents/{agent.id}#activityLog",
                            ref_id=approval.id,
                        )
                except (ValueError, AttributeError):
                    pass  # Invalid UUID, skip

        await db.flush()
        return approval

    async def _execute_approved_action(
        self,
        agent_id: uuid.UUID,
        action_type: str,
        details: dict,
        resolved_by_user_id: uuid.UUID | None = None,
    ) -> str | None:
        """Execute the tool action that was approved.

        Reads the tool name and arguments from the approval details,
        then directly calls the tool executor (bypassing autonomy check).
        """
        tool_name = details.get("tool")
        args_raw = details.get("args", "{}")
        if not tool_name:
            return None

        try:
            # Parse args — stored as str(dict) so we need ast.literal_eval
            import ast
            if isinstance(args_raw, str):
                try:
                    arguments = ast.literal_eval(args_raw)
                except (ValueError, SyntaxError):
                    try:
                        arguments = json.loads(args_raw)
                    except json.JSONDecodeError:
                        arguments = {}
            else:
                arguments = args_raw

            # Import and call the tool's direct executor (no autonomy re-check)
            from app.services.agent_tools import _execute_tool_direct
            result = await _execute_tool_direct(
                tool_name,
                arguments,
                agent_id,
                user_id=resolved_by_user_id,
            )
            return result
        except Exception as e:
            logger.error(f"Failed to execute approved action {tool_name}: {e}")
            return f"Execution failed: {e}"

    async def _notify_creator(self, db: AsyncSession, agent: Agent,
                               action_type: str, details: dict) -> list[dict]:
        """Send L2 notification to agent creator via Feishu + web."""
        # Web notification (always)
        from app.services.notification_service import send_notification
        safe_agent_name = _safe_user_label(agent.name, "Agent")
        safe_action_type = _safe_user_label(action_type, "action")
        await send_notification(
            db,
            user_id=agent.creator_id,
            type="autonomy_l2",
            title=f"{safe_agent_name}: executed {safe_action_type}",
            body=sanitize_user_visible_text(json.dumps(details, ensure_ascii=False)[:200]),
            link=f"/agents/{agent.id}#activityLog",
        )

        # Try Feishu notification if channel is configured
        channel_result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "feishu",
                ChannelConfig.is_configured.is_(True),
            )
        )
        channel = channel_result.scalars().first()

        pending_notifications: list[dict] = []
        if channel and channel.app_id and channel.app_secret:
            creator_result = await db.execute(
                select(User).where(User.id == agent.creator_id)
            )
            creator = creator_result.scalar_one_or_none()
            if creator:
                from app.models.identity import IdentityProvider
                from app.models.org import OrgMember

                provider_r = await db.execute(
                    select(IdentityProvider).where(
                        IdentityProvider.provider_type == "feishu",
                        IdentityProvider.tenant_id == creator.tenant_id,
                    )
                )
                provider = provider_r.scalar_one_or_none()
                if provider:
                    member_r = await db.execute(
                        select(OrgMember).where(
                            OrgMember.user_id == creator.id,
                            OrgMember.provider_id == provider.id,
                        )
                    )
                    member = member_r.scalar_one_or_none()
                    if member and (member.external_id or member.open_id):
                        receive_id = member.external_id or member.open_id
                        id_type = "user_id" if member.external_id else "open_id"
                        pending_notifications.append(await _prepare_creator_feishu_message(
                            db=db,
                            agent=agent,
                            creator=creator,
                            receive_id=receive_id,
                            receive_id_type=id_type,
                            text_content=f"{safe_agent_name}: executed {safe_action_type}",
                            artifact_role="autonomy_notification",
                        ))
        return pending_notifications

    async def _request_approval(self, db: AsyncSession, agent: Agent,
                                 approval: ApprovalRequest) -> list[dict]:
        """Send L3 approval request to creator via Feishu card + web notification."""
        # Web notification (always)
        from app.services.notification_service import send_notification
        safe_agent_name = _safe_user_label(agent.name, "Agent")
        safe_action_type = _safe_user_label(approval.action_type, "action")
        safe_details = sanitize_user_visible_text(
            json.dumps(approval.details, ensure_ascii=False)
        )
        await send_notification(
            db,
            user_id=agent.creator_id,
            type="approval_pending",
            title=f"{safe_agent_name}: requests approval for {safe_action_type}",
            body=safe_details[:200],
            link=f"/agents/{agent.id}#approvals",
            ref_id=approval.id,
        )

        # Try Feishu notification
        channel_result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "feishu",
                ChannelConfig.is_configured.is_(True),
            )
        )
        channel = channel_result.scalars().first()

        pending_notifications: list[dict] = []
        if channel and channel.app_id and channel.app_secret:
            creator_result = await db.execute(
                select(User).where(User.id == agent.creator_id)
            )
            creator = creator_result.scalar_one_or_none()
            if creator:
                from app.models.identity import IdentityProvider
                from app.models.org import OrgMember

                provider_r = await db.execute(
                    select(IdentityProvider).where(
                        IdentityProvider.provider_type == "feishu",
                        IdentityProvider.tenant_id == creator.tenant_id,
                    )
                )
                provider = provider_r.scalar_one_or_none()
                if provider:
                    member_r = await db.execute(
                        select(OrgMember).where(
                            OrgMember.user_id == creator.id,
                            OrgMember.provider_id == provider.id,
                        )
                    )
                    member = member_r.scalar_one_or_none()
                    if member and (member.external_id or member.open_id):
                        receive_id = member.external_id or member.open_id
                        pending_notifications.append(await _prepare_creator_feishu_message(
                            db=db,
                            agent=agent,
                            creator=creator,
                            receive_id=receive_id,
                            receive_id_type=("user_id" if member.external_id else "open_id"),
                            text_content=(
                                f"🔴 {safe_agent_name}: 请求审批\n"
                                f"操作: {safe_action_type}\n"
                                f"详情: {safe_details}\n\n"
                                "请在平台审批。"
                            ),
                            artifact_role="autonomy_approval",
                        ))
        return pending_notifications


autonomy_service = AutonomyService()
