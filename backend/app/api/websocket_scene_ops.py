"""Scene and history helpers for websocket chat."""

from __future__ import annotations

import uuid

from app.models.chat_session import ChatSession
from app.services.scene_activation import resolve_session_scene


def public_manifest(manifest):
    from app.services.scene_service import project_scene_manifest

    return project_scene_manifest(manifest) if manifest else None


async def load_scene_manifest_impl(api, self, *, db=None) -> None:
    self.scene_manifest = None
    try:
        from app.schemas.scene import validate_scene_key
        from app.services.scene_service import SCENE_STATUS_OK, resolve_scene_for_activation

        if self.scene_key:
            self.scene_key = validate_scene_key(self.scene_key)

        async def _load(active_db):
            session_id = getattr(self, "conv_id", None)
            session = await active_db.get(ChatSession, uuid.UUID(str(session_id))) if session_id else None
            manifest = await resolve_session_scene(active_db, self.agent_id, session, explicit_key=self.scene_key)
            # H5 sends an empty scene parameter for its existing default landing.
            if manifest is None and self.scene_key == "":
                resolved = await resolve_scene_for_activation(active_db, self.agent_id, "default")
                return resolved.manifest if resolved.status == SCENE_STATUS_OK else None
            return manifest

        if db is not None:
            self.scene_manifest = await _load(db)
        else:
            async with api.async_session() as active_db:
                self.scene_manifest = await _load(active_db)
    except (TypeError, ValueError):
        api.logger.warning(f"[WS] Ignoring invalid scene key: {self.scene_key!r}")
        self.scene_key = None
    except Exception as exc:
        api.logger.warning(f"[WS] Scene load failed (non-fatal): {exc}")


def has_configured_scene_welcome_impl(_api, self) -> bool:
    if not self.scene_manifest:
        return False
    return bool(str(self.scene_manifest.get("welcome_message") or "").strip())


def resolve_onboarding_required_impl(_api, self, onboarding_required: bool) -> bool:
    if getattr(self, "host_context", False):
        return False
    automatic = (self.scene_manifest or {}).get("activation_source") == "automatic"
    return bool(onboarding_required and not automatic and not self._has_configured_scene_welcome())


async def prepare_initial_greeting_impl(api, self, db, user_id) -> None:
    self.pending_initial_assistant = None
    if getattr(self, "host_context", False) or self.history_messages or not self._has_configured_scene_welcome():
        return
    if not await api.claim_fixed_welcome_slot(db, self.agent_id, user_id):
        api.logger.info("[WS] Fixed scene welcome skipped because onboarding already published visible output")
        return
    self.pending_initial_assistant = {
        "content": str(self.scene_manifest["welcome_message"]),
        "message_meta": {
            **self._scene_message_meta(),
            "scene_welcome": True,
        },
    }


def scene_message_meta_impl(_api, self) -> dict:
    from app.services.scene_service import scene_message_meta

    return scene_message_meta(self.scene_manifest) if self.scene_manifest else {"scene_resolved": True}


def channel_context_impl(_api, self) -> dict:
    from app.services.scene_service import build_scene_channel_context

    if self.source_channel in {"miniprogram", "wechat_miniprogram"}:
        display_name = "小程序"
        client_surface = "mini-program web-view"
    else:
        display_name = "Web"
        client_surface = "desktop web"
    return build_scene_channel_context(
        self.scene_manifest,
        source_channel=self.source_channel,
        display_name=display_name,
        client_surface=client_surface,
    )


async def load_history_impl(api, self, db):
    try:
        from app.services.chat_history import load_messages_for_session

        self.history_messages = await load_messages_for_session(
            db,
            agent_id=self.agent_id,
            conversation_id=self.conv_id,
            ctx_size=self.ctx_size,
        )
        api.logger.info(f"[WS] Loaded {len(self.history_messages)} history messages for session {self.conv_id}")
    except Exception as e:
        api.logger.warning(f"[WS] History load failed (non-fatal): {e}")


def build_conversation_context_impl(_api, self) -> list[dict]:
    from app.services.chat_history import build_llm_messages_from_rows

    conversation: list[dict] = build_llm_messages_from_rows(self.history_messages, include_thinking=True)
    return conversation
