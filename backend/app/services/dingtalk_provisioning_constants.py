"""Constants for DingTalk automatic channel provisioning."""

DINGTALK_BINDING_WELCOME_FALLBACK_NAME = "你的数字员工"
DINGTALK_PROVISIONING_OPERATION_INITIAL = "initial_setup"
DINGTALK_PROVISIONING_OPERATION_FORCE = "force_reconfigure"
DINGTALK_REGISTRATION_TERMINAL_STATUSES = frozenset(
    {
        "SUCCESS",
        "FAIL",
        "EXPIRED",
    }
)

# The first send is picked up immediately after the credential transaction
# commits. DingTalk's automatically granted robot permission is eventually
# consistent, so retry the completion message for a bounded period without
# delaying channel setup. Including the initial attempt, this allows nine sends
# over about seven minutes. State persists on the provisioning row.
DINGTALK_WELCOME_RETRY_DELAYS_SECONDS = (2, 5, 10, 20, 30, 60, 120, 180)
