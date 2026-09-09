"""Connection setup and session-resolution operations for websocket chat."""

from __future__ import annotations


async def safe_send_impl(api, self, payload: dict):
    try:
        await api.manager.send_to_session(str(self.agent_id), self.conv_id, payload)
        if payload.get("event_kind") == "turn_terminal" and payload.get("message_id"):
            from app.services.turn_delivery_recovery import acknowledge_terminal_event

            await acknowledge_terminal_event(payload["message_id"])
    except Exception:
        pass


async def run_impl(api, self):
    try:
        success = await self.setup()
        if not success:
            return
        await self.message_loop()
    except api.WebSocketDisconnect:
        api.logger.info(f"[WS] Client disconnected: {self.user_id or 'unknown'}")
        await api.manager.disconnect(str(self.agent_id), self.websocket)
    except Exception as e:
        api.logger.exception(f"[WS] Unexpected error: {e}")
        await api.manager.disconnect(str(self.agent_id), self.websocket)


async def setup_impl(api, self) -> bool:
    await self.websocket.accept()

    try:
        payload = api.decode_access_token(self.token)
        user_id = api.uuid.UUID(payload["sub"])
        self.user_id = user_id
    except Exception:
        await self.websocket.send_json({"type": "error", "content": "Authentication failed"})
        await self.websocket.close(code=4001)
        return False

    try:
        async with api.async_session() as db:
            result = await db.execute(api.select(api.User).where(api.User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                api.logger.error("[WS] User not found")
                await self.websocket.send_json({"type": "error", "content": "User not found"})
                await self.websocket.close(code=4001)
                return False

            api.logger.info(f"[WS] Checking agent access for {self.agent_id}")
            project_session = None
            agent_access = None
            if self.session_id_param:
                try:
                    project_session = await db.get(api.ChatSession, api.uuid.UUID(self.session_id_param))
                except (TypeError, ValueError):
                    project_session = None
            if project_session is not None and project_session.agent_id == self.agent_id:
                from app.services.project_service import project_session_access_mode

                self.project_session_access = await project_session_access_mode(db, user, project_session)
            if self.project_session_access is not None:
                agent = await db.get(api.Agent, self.agent_id)
                if agent is None or agent.is_deleted or agent.tenant_id != user.tenant_id:
                    await self.websocket.send_json({"type": "error", "content": "Session not found"})
                    await self.websocket.close(code=4003)
                    return False
            else:
                agent, agent_access = await api.check_agent_access(db, user, self.agent_id)
            api.require_current_agent_tenant(user, agent)
            if api.is_agent_expired(agent):
                await self.websocket.send_json(
                    {
                        "type": "error",
                        "content": "数字员工已过期并停止服务，请联系管理员延长有效期。",
                    }
                )
                await self.websocket.close(code=4003)
                return False

            self.agent_name = agent.name
            self.agent_type = agent.agent_type or ""
            self.role_description = agent.role_description or ""
            self.welcome_message = agent.welcome_message or ""
            self.ctx_size = agent.context_window_size or 100
            self.user_display_name = (user.display_name or "").strip() or "there"
            self.tenant_id = user.tenant_id
            api.logger.info(
                f"[WS] Agent: {self.agent_name}, type: {self.agent_type}, model_id: {agent.primary_model_id}, ctx: {self.ctx_size}"
            )

            await self._load_models(db, agent)

            self.conv_id = await self._resolve_chat_session(
                db,
                user_id,
                viewer=user,
                agent=agent,
                agent_access=agent_access,
            )
            if not self.conv_id:
                return False

            await self._load_scene_manifest(db)
            await self._load_history(db)
            await self._prepare_initial_greeting(db, user_id)
            if not getattr(self, "host_context", False):
                onboarding_eligibility = await api.resolve_onboarding_eligibility(
                    db,
                    self.agent_id,
                    user_id,
                    api.uuid.UUID(self.conv_id),
                )
                self.onboarding_required = self._resolve_onboarding_required(onboarding_eligibility.required)
            await db.commit()
    except Exception as e:
        api.logger.exception(f"[WS] Setup error: {e}")
        await self.websocket.send_json({"type": "error", "content": "Setup failed"})
        await self.websocket.close(code=4002)
        return False

    from starlette.websockets import WebSocketState as _WSState

    def _ws_dead(ws) -> bool:
        return (
            getattr(ws, "client_state", None) == _WSState.DISCONNECTED
            or getattr(ws, "application_state", None) == _WSState.DISCONNECTED
        )

    agent_id_str = str(self.agent_id)
    _conns = api.manager.active_connections.setdefault(agent_id_str, [])
    _conns[:] = [(ws, sid, uid) for ws, sid, uid in _conns if not _ws_dead(ws)]
    await api.manager.connect(agent_id_str, self.websocket, self.conv_id, str(user_id))
    api.logger.info(f"[WS] Ready! Agent={self.agent_name} (live conns for agent: {len(_conns)})")

    turn_snapshot = await self._load_turn_snapshot()
    await self.websocket.send_json(
        api.with_turn_envelope(
            {
                "type": "connected",
                "session_id": self.conv_id,
                "read_only": self.read_only,
                "source_channel": self.source_channel,
                "onboarding_required": self.onboarding_required,
                "scene_manifest": api.websocket_scene_ops.public_manifest(self.scene_manifest),
            },
            turn_snapshot,
            event_kind="turn_snapshot",
        )
    )

    self.conversation = self._build_conversation_context()
    return True


async def load_turn_snapshot_impl(
    api,
    self,
    *,
    turn_anchor_id=None,
):
    async with api.async_session() as db:
        return await api.get_conversation_turn_snapshot(
            db,
            agent_id=self.agent_id,
            conversation_id=self.conv_id,
            turn_anchor_id=turn_anchor_id,
        )


async def transition_turn_impl(api, self, turn_anchor_id, status: str):
    if turn_anchor_id is None:
        return None
    async with api.async_session() as db:
        snapshot = await api.transition_conversation_turn(
            db,
            agent_id=self.agent_id,
            conversation_id=self.conv_id,
            turn_anchor_id=turn_anchor_id,
            status=status,
        )
        await db.commit()
    return snapshot


async def publish_turn_lifecycle_impl(api, self, snapshot) -> None:
    if snapshot is None:
        return
    await self._safe_send(
        api.with_turn_envelope(
            {"type": "turn_state"},
            snapshot,
            event_kind="turn_lifecycle",
        )
    )


async def send_current_turn_event_impl(
    api,
    self,
    payload: dict,
    *,
    event_kind: str = "turn_rejected",
    reject_attempt: bool = True,
) -> None:
    snapshot = await self._load_turn_snapshot()
    normalized_payload = dict(payload)
    if reject_attempt and self.current_client_message_id:
        normalized_payload.setdefault("rejected_message_id", self.current_client_message_id)
    await self.websocket.send_json(
        api.with_turn_envelope(normalized_payload, snapshot, event_kind=event_kind)
    )


async def load_models_impl(api, self, db, agent):
    from app.services.chat_model_selection import resolve_runtime_models

    resolved = await resolve_runtime_models(db, agent=agent)
    self.llm_model = resolved.primary_model
    self.fallback_llm_model = resolved.fallback_model
    api.logger.info(
        f"[WS] Models loaded: primary="
        f"{self.llm_model.model if self.llm_model else 'None'}, fallback="
        f"{self.fallback_llm_model.model if self.fallback_llm_model else 'None'}"
    )


async def resolve_chat_session_impl(
    api, self, db, user_id, *, viewer, agent, agent_access=None
) -> str | None:
    from app.services.session_query import build_owned_sessions_predicate

    conv_id = self.session_id_param
    if conv_id:
        try:
            _sid = api.uuid.UUID(conv_id)
        except (ValueError, TypeError):
            conv_id = None
            _existing = None
        else:
            _sr = await db.execute(
                api.select(api.ChatSession).where(
                    api.ChatSession.id == _sid,
                    build_owned_sessions_predicate(self.agent_id),
                )
            )
            _existing = _sr.scalar_one_or_none()
            if not _existing:
                conv_id = None
            else:
                if bool(dict(_existing.im_config or {}).get("read_only")):
                    self.read_only = True
                if self.project_session_access is not None and _existing.source_channel != "subagent":
                    self.read_only = self.read_only or self.project_session_access != "edit"
                is_subagent_owner = False
                if _existing.source_channel == "subagent":
                    if self.project_session_access is not None:
                        self.read_only = self.read_only or self.project_session_access != "edit"
                    else:
                        self.read_only = True
                    run = await db.get(api.SubagentRun, _existing.id)
                    is_subagent_owner = bool(
                        self.project_session_access is not None
                        or (run is not None and run.execution_user_id == user_id)
                    )
                if hasattr(agent, "tenant_id"):
                    try:
                        await api.require_tenant_safe_chat_session(db, _existing, agent.tenant_id)
                    except api.HTTPException:
                        await self.websocket.send_json({"type": "error", "content": "Session not found"})
                        await self.websocket.close(code=4003)
                        return None
                self.source_channel = _existing.source_channel or self.source_channel
            if (
                _existing
                and _existing.source_channel != "agent"
                and str(_existing.user_id) != str(user_id)
                and not is_subagent_owner
                and self.project_session_access is None
            ):
                if api.can_view_all_agent_chat_sessions(viewer, agent, agent_access):
                    self.read_only = True
                else:
                    await self.websocket.send_json({"type": "error", "content": "Not authorized for this session"})
                    await self.websocket.close(code=4003)
                    return None
    if not conv_id:
        _latest = await api.ensure_primary_platform_session(
            db,
            self.agent_id,
            user_id,
            source_channel=self.source_channel,
        )
        conv_id = str(_latest.id)
        api.logger.info(f"[WS] Selected primary session {conv_id}")
    return conv_id


async def project_session_still_writable_impl(api, self) -> bool:
    if getattr(self, "project_session_access", None) is None or not self.conv_id or self.user_id is None:
        return not self.read_only
    async with api.async_session() as db:
        session = await db.get(api.ChatSession, api.uuid.UUID(self.conv_id))
        user = await db.get(api.User, self.user_id)
        if session is None or user is None:
            return False
        from app.services.project_service import project_session_access_mode

        return await project_session_access_mode(db, user, session) == "edit"


async def enqueue_project_subagent_message_impl(
    api,
    self,
    *,
    content: str,
    display_content: str,
    file_name: str,
    client_message_id: str | None,
    attachments: list[dict] | None,
) -> bool:
    if self.source_channel != "subagent" or self.project_session_access is None:
        return False
    if not self.conv_id or self.user_id is None:
        await self._send_current_turn_event({"type": "error", "content": "项目工作会话无效。"})
        return True

    try:
        child_id = api.uuid.UUID(self.conv_id)
    except (TypeError, ValueError):
        await self._send_current_turn_event({"type": "error", "content": "项目工作会话无效。"})
        return True

    from app.models.project import ProjectRun
    from app.services.subagent_runtime import SubagentError, append_subagent_message

    async with api.async_session() as db:
        child = await db.get(api.ChatSession, child_id)
        run = await db.get(api.SubagentRun, child_id)
        if (
            child is None
            or run is None
            or child.source_channel != "subagent"
            or child.project_id is None
            or run.project_id != child.project_id
            or child.agent_id != self.agent_id
        ):
            await self._send_current_turn_event({"type": "error", "content": "项目工作会话无效。"})
            return True

        active_project_runs = (
            (
                await db.execute(
                    api.select(ProjectRun)
                    .where(
                        ProjectRun.project_id == child.project_id,
                        ProjectRun.agent_id == child.agent_id,
                        ProjectRun.status.in_(["queued", "running", "waiting"]),
                    )
                    .order_by(ProjectRun.created_at.desc(), ProjectRun.id.desc())
                )
            )
            .scalars()
            .all()
        )
        project_run_id = next(
            (
                row.id
                for row in active_project_runs
                if str(dict(row.output or {}).get("subagent_session_id") or "") == str(child_id)
            ),
            None,
        )
        parent_session_id = str(run.parent_session_id)
        execution_user_id = run.execution_user_id

    raw_client_id = str(client_message_id or "").strip()
    durable_message_key = (
        api.uuid.uuid5(
            api.uuid.NAMESPACE_URL,
            f"clawith:project-subagent-web:{child_id}:{raw_client_id}",
        )
        if raw_client_id
        else api.uuid.uuid4()
    )
    origin_tool_call_id = f"project-web:{durable_message_key}"
    event_key = f"subagent-parent-input:{child_id}:{origin_tool_call_id}"
    try:
        await append_subagent_message(
            agent_id=self.agent_id,
            parent_session_id=parent_session_id,
            subagent_id=str(child_id),
            message=content,
            execution_user_id=execution_user_id,
            origin_tool_call_id=origin_tool_call_id,
            project_run_id=project_run_id,
            input_metadata={
                "project_web_input": True,
                "web_sender_user_id": str(self.user_id),
                "client_message_id": raw_client_id or None,
                "display_content": display_content or None,
                "file_name": file_name or None,
                "attachments": list(attachments or []),
            },
            allow_parent_continuation=True,
        )
    except SubagentError as exc:
        async with api.async_session() as db:
            from app.services.confirmation_service import find_pending_confirmation

            pending = await find_pending_confirmation(
                db,
                agent_id=self.agent_id,
                conversation_id=self.conv_id,
            )
        if pending is not None and pending.force_confirmation:
            await self._send_current_turn_event(
                {
                    "type": "confirmation_required",
                    "content": "请先完成待确认操作。",
                    "message_id": raw_client_id or None,
                    "name": "request_confirmation",
                    "call_id": str(pending.row_id),
                    "args": pending.args,
                    "status": "running",
                }
            )
        else:
            await self._send_current_turn_event({"type": "error", "content": str(exc)})
        return True

    async with api.async_session() as db:
        persisted = (
            await db.execute(
                api.select(api.ChatMessage).where(api.ChatMessage.external_event_key == event_key)
            )
        ).scalar_one_or_none()
        snapshot = await api.get_conversation_turn_snapshot(
            db,
            agent_id=self.agent_id,
            conversation_id=self.conv_id,
        )
    if persisted is not None:
        metadata = dict(persisted.message_meta or {})
        await api.publish_conversation_turn_event(
            agent_id=self.agent_id,
            conversation_id=self.conv_id,
            payload={
                "type": "user_message_committed",
                **({"client_message_id": raw_client_id} if raw_client_id else {}),
                "message_id": str(persisted.id),
                "id": str(persisted.id),
                "role": "user",
                "content": persisted.content,
                "display_content": metadata.get("display_content") or persisted.content,
                "attachments": list(metadata.get("attachments") or []),
                "sender_user_id": str(persisted.sender_user_id) if persisted.sender_user_id is not None else None,
                "created_at": persisted.created_at.isoformat() if persisted.created_at is not None else None,
            },
            snapshot=snapshot,
            event_kind="turn_user_committed",
        )
    return True
