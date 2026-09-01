"""Confirmation lifecycle facade preserving the original public module contract."""

from functools import wraps

from app.services import confirmation_core as _confirmation_core
from app.services import confirmation_delivery as _confirmation_delivery
from app.services.confirmation_shared import *  # noqa: F401,F403
from app.services.confirmation_core import *  # noqa: F401,F403
from app.services.confirmation_delivery import *  # noqa: F401,F403


def _sync_confirmation_dependencies() -> None:
    for module in (_confirmation_core, _confirmation_delivery):
        for name in (
            "_broadcast",
            "_mark_card_expired",
            "_push_card_state",
            "_reenter_loop",
            "_update_origin_card",
            "deliver_reply_to_origin",
            "redeliver_pending_confirmation",
            "resolve_confirmation",
        ):
            if name in globals():
                setattr(module, name, globals()[name])


def _confirmation_proxy(function):
    @wraps(function)
    async def proxy(*args, **kwargs):
        _sync_confirmation_dependencies()
        return await function(*args, **kwargs)

    proxy.__module__ = __name__
    return proxy


for _proxy_name in (
    "suspend_for_confirmation",
    "resolve_confirmation",
    "_reenter_loop",
    "_deliver_channel_card",
    "_deliver_channel_card_unlocked",
    "redeliver_pending_confirmation",
    "cancel_pending_confirmation_for_stop",
    "ignore_pending_confirmation_for_new_input",
    "publish_ignored_confirmation",
    "_update_origin_card",
    "resolve_confirmation_via_dingtalk",
    "_mark_card_expired",
    "_push_card_state",
):
    _implementation = getattr(
        _confirmation_core
        if hasattr(_confirmation_core, _proxy_name)
        else _confirmation_delivery,
        _proxy_name,
    )
    globals()[_proxy_name] = _confirmation_proxy(_implementation)


_COMPAT_SYMBOLS = ('_im_send_lock_key', 'ConfirmationActorMismatch', 'PendingConfirmation', 'find_pending_confirmation', 'find_dingtalk_pending_confirmation', '_assert_confirmation_actor', '_action_preview', '_build_card_args', '_ago', '_broadcast', '_resolve_session_channel', 'suspend_for_confirmation', 'resolve_confirmation', '_reenter_loop', '_deliver_channel_card', '_deliver_channel_card_unlocked', 'redeliver_pending_confirmation', 'cancel_pending_confirmation_for_stop', 'ignore_pending_confirmation_for_new_input', 'publish_ignored_confirmation', '_mark_selected_buttons', '_update_origin_card', 'resolve_confirmation_via_dingtalk', '_mark_card_expired', '_push_card_state')
for _compat_name in _COMPAT_SYMBOLS:
    _compat_symbol = globals().get(_compat_name)
    if _compat_symbol is not None:
        _compat_symbol.__module__ = __name__
