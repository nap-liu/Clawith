# Agent Contact Discovery and DingTalk Directory Sync Design

## Background

Clawith currently treats `AgentRelationship` as the outbound human-contact allow-list. Tools such as `send_channel_message` and gateway `send-message` only deliver messages to active relationships. That keeps outbound communication controlled, but it forces users or admins to manually configure contacts before an agent can proactively reach the right person.

The new model keeps `AgentRelationship` as the outbound safety boundary, while giving agents controlled builtin tools to discover and add contacts from the tenant directory. DingTalk directory sync provides the initial contact source.

## Goals

- Sync DingTalk enterprise contacts into local `OrgMember` rows without requiring DingTalk SSO login to be enabled.
- Add explicit UI semantics: DingTalk credentials can be used for directory sync only; SSO login is controlled by a separate switch.
- Introduce agent builtin tools for contact discovery and contact addition.
- Keep `send_channel_message` gated by active `AgentRelationship` rows.
- Store enough audit data to understand which agent added which contact and why.

## Non-Goals

- Do not sync personal DingTalk friend/contact lists. Only enterprise directory contacts in the app's authorized scope are supported.
- Do not let agents directly message arbitrary `OrgMember` rows without adding a relationship first.
- Do not expose phone numbers or other sensitive matching fields to the LLM tool result.
- Do not implement a full approval workflow in the first iteration. The first version uses policy-gated direct add.

## Data Model

Existing tables remain the main source of truth:

- `identity_providers`: stores DingTalk app credentials, tenant binding, and `sso_login_enabled`.
- `org_departments`: synced department tree.
- `org_members`: synced people. For DingTalk:
  - `external_id` stores DingTalk `userid`.
  - `unionid` stores DingTalk `unionid`.
  - `phone` stores normalized mobile when permission is available.
  - `provider_id` points to the DingTalk `IdentityProvider`.
  - `user_id` links to a platform `User` when email or phone matching succeeds.
- `agent_relationships`: active agent-to-human contact allow-list.

First iteration metadata should use existing `AgentRelationship` fields:

- `relation`: default `collaborator`.
- `description`: include the agent-supplied reason, prefixed with `Agent-added contact:`.
- `created_by_user_id` and `updated_by_user_id`: use the agent creator or the acting manager where available.

If richer audit is needed later, add `agent_relationship_events` rather than overloading `description`.

## DingTalk Directory Sync

Directory sync uses DingTalk enterprise internal app credentials, not personal OAuth login:

1. Admin creates or configures a DingTalk enterprise internal app.
2. Admin enables Contacts capability and grants department/member read permissions.
3. Admin grants personal mobile/email fields if phone/email matching is required.
4. Admin stores `app_key` and `app_secret` in the DingTalk identity provider config.
5. Admin can leave `sso_login_enabled=false` and still run directory sync.

The existing `DingTalkOrgSyncAdapter` already uses:

- `topapi/v2/department/listsub` for recursive department traversal.
- `topapi/v2/user/list` for department member detail pagination.

The first implementation step should tighten normalization and sync-only semantics:

- Normalize DingTalk mobile values before matching platform identities.
- Treat `sso_login_enabled` as login-only, never as a prerequisite for org sync.
- Update UI copy so the DingTalk provider reads as "Directory sync credentials" and the SSO switch is clearly optional.

## Builtin Tools

### `search_contacts`

Purpose: let an agent find possible human contacts from the local tenant directory.

Input:

```json
{
  "query": "string",
  "department": "string optional",
  "channel": "dingtalk|feishu|wecom|platform optional",
  "limit": 10
}
```

Output:

```json
{
  "contacts": [
    {
      "contact_id": "org_member_uuid",
      "name": "Zhang San",
      "title": "Product Manager",
      "department_path": "Product/Platform",
      "channels": ["dingtalk"],
      "already_contact": false,
      "match_reason": "name match"
    }
  ]
}
```

Rules:

- Search only `OrgMember.tenant_id == agent.tenant_id`.
- Search only `OrgMember.status == "active"`.
- For `private` and `custom` agents, restrict platform-linked contacts by existing access policy.
- Do not return phone numbers.
- Limit results to a small bounded number, default 10 and max 20.

### `add_contact`

Purpose: let an agent add a discovered contact to its relationship network.

Input:

```json
{
  "contact_id": "org_member_uuid",
  "relation": "collaborator",
  "reason": "Need to ask for Q3 billing numbers"
}
```

Output:

```json
{
  "status": "added",
  "contact": {
    "name": "Zhang San",
    "channels": ["dingtalk"]
  }
}
```

Rules:

- Require a non-empty `reason`.
- Validate tenant isolation.
- Validate contact status is active.
- For first iteration, allow direct add only when the source agent is company-visible, or when policy already grants access to the linked platform user.
- Idempotently return `already_exists` if the relationship already exists.
- Do not send a message as a side effect.

## Message Flow

The intended agent flow is:

1. `search_contacts(query="finance owner")`
2. `add_contact(contact_id="...", reason="Need finance owner to confirm invoice status")`
3. `send_channel_message(member_name="...", message="...")`

`send_channel_message` remains unchanged as the final enforcement point. If a contact was not added, sending still fails.

## UI Changes

In Company Settings > Org / Identity Providers:

- DingTalk config should label `App Key` and `App Secret` as usable for directory sync.
- Add explanatory copy: "Saving DingTalk credentials enables directory sync. SSO login remains disabled unless the SSO Login switch is turned on."
- Keep the existing SSO toggle, but make it visually and textually separate from directory sync setup.
- Update DingTalk setup guide to split:
  - Directory sync steps.
  - Optional SSO login steps.

## Security and Privacy

- Relationships remain the outbound allow-list.
- Agent-created relationships must include a reason.
- Contact search must not leak phone numbers.
- All contact queries must be tenant-scoped.
- Provider scope controls the reachable universe. DingTalk contacts outside the app authorization scope cannot be discovered or messaged.

## Iteration Plan

1. DingTalk sync-only provider semantics and mobile normalization.
2. `search_contacts` service and builtin tool.
3. `add_contact` service and builtin tool.
4. Gateway parity for OpenClaw agents.
5. Frontend visibility for agent-created contacts and policy toggles.
