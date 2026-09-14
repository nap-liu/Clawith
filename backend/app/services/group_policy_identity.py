"""Stable group/member references; conversation resets are not identity changes."""

import json
import uuid

GROUP_PREFIXES = {"dingtalk": "dingtalk_group_", "feishu": "feishu_group_",
                  "wecom": "wecom_group_", "slack": "slack_"}


def external_group_from_route(channel, route):
    prefix = GROUP_PREFIXES.get(channel)
    route = route or ""
    if not prefix or not route.startswith(prefix) or "__archived_" in route or "__control_" in route:
        return None
    return route[len(prefix):] or None


def stable_group_id(agent_id, channel, scope, external_id):
    return uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
        [str(agent_id), channel, scope, external_id], separators=(",", ":"),
    ))


def stable_member_id(group_id, subject_type, subject):
    return uuid.uuid5(group_id, json.dumps([subject_type, subject], separators=(",", ":")))
