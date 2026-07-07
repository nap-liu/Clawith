# DingTalk Contact Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first slice of agent-managed contacts by making DingTalk directory sync usable without DingTalk SSO login and preparing the contact discovery architecture.

**Architecture:** DingTalk `IdentityProvider` credentials are shared by directory sync and optional SSO login, but `sso_login_enabled` controls only login. Directory sync writes tenant-scoped `OrgMember` records; future `search_contacts` and `add_contact` tools operate on those local rows and keep `AgentRelationship` as the outbound allow-list.

**Tech Stack:** FastAPI, SQLAlchemy async ORM, pytest, React 19, TypeScript, TanStack Query, i18next.

---

## File Structure

- Modify `backend/app/services/org_sync_adapter.py`: normalize mobile values before matching and storing DingTalk/other provider contacts.
- Modify `backend/tests/test_org_sync_adapter.py`: cover contact normalization and matching behavior.
- Modify `frontend/src/pages/enterprise-settings/tabs/OrgTab.tsx`: clarify DingTalk sync-only setup and optional SSO login switch.
- Modify `frontend/src/i18n/zh.json`: Chinese DingTalk copy for sync-only credentials and optional login.
- Modify `frontend/src/i18n/en.json`: English DingTalk copy for sync-only credentials and optional login.
- Later create `backend/app/services/contact_discovery_service.py`: search and add contact services.
- Later create `backend/tests/test_contact_discovery_service.py`: search/add contact policy tests.
- Later modify `backend/app/services/agent_tools.py`: register `search_contacts` and `add_contact`.

## Task 1: DingTalk Sync-Only Semantics and Mobile Normalization

**Files:**
- Modify: `backend/app/services/org_sync_adapter.py`
- Modify: `backend/tests/test_org_sync_adapter.py`
- Modify: `frontend/src/pages/enterprise-settings/tabs/OrgTab.tsx`
- Modify: `frontend/src/i18n/zh.json`
- Modify: `frontend/src/i18n/en.json`

- [x] **Step 1: Write failing backend tests for contact normalization**

Add tests to `backend/tests/test_org_sync_adapter.py`:

```python
from app.services.org_sync_adapter import normalize_contact_for_match


def test_normalize_contact_for_match_strips_common_mobile_formatting():
    assert normalize_contact_for_match("+86 138-0013-8000") == "8613800138000"
    assert normalize_contact_for_match(" 138 0013 8000 ") == "13800138000"


def test_normalize_contact_for_match_keeps_email_lowercase():
    assert normalize_contact_for_match(" Alice@Example.COM ") == "alice@example.com"
```

- [x] **Step 2: Run backend test and verify RED**

Run:

```bash
python3 -m pytest backend/tests/test_org_sync_adapter.py -q
```

Expected: fails because `normalize_contact_for_match` is not defined. If local Python has no pytest, run the same command in the backend Docker container used by the local 3008 stack.

- [x] **Step 3: Implement minimal normalization helper**

In `backend/app/services/org_sync_adapter.py`, replace `_normalize_contact` with:

```python
def normalize_contact_for_match(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if "@" in value:
        return value.lower()
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or value
```

Update `_upsert_member` to call `normalize_contact_for_match` for `email` and `mobile`.

- [x] **Step 4: Run backend test and verify GREEN**

Run:

```bash
python3 -m pytest backend/tests/test_org_sync_adapter.py -q
```

Expected: all tests in `test_org_sync_adapter.py` pass.

- [x] **Step 5: Update DingTalk provider UI copy**

In the DingTalk branch of `renderProviderForm`, add a small sync-only notice above App Key:

```tsx
<div className="form-group" style={{ gridColumn: '1 / -1' }}>
    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
        {t('enterprise.identity.dingtalkSyncOnlyHint', 'These DingTalk credentials can be used for directory sync only. SSO login stays off unless you enable the SSO Login switch below.')}
    </div>
</div>
```

Update DingTalk setup guide copy so SSO steps are explicitly optional.

- [x] **Step 6: Run frontend build check**

Run:

```bash
cd frontend && npm run build
```

Expected: Vite build completes without TypeScript errors.

- [x] **Step 7: Commit Task 1**

```bash
git add backend/app/services/org_sync_adapter.py backend/tests/test_org_sync_adapter.py frontend/src/pages/enterprise-settings/tabs/OrgTab.tsx frontend/src/i18n/zh.json frontend/src/i18n/en.json
git commit -m "feat: clarify dingtalk sync-only identity provider"
```

## Task 2: Contact Discovery Service

**Files:**
- Create: `backend/app/services/contact_discovery_service.py`
- Create: `backend/tests/test_contact_discovery_service.py`

- [ ] **Step 1: Write failing tests for search visibility**

Create `backend/tests/test_contact_discovery_service.py` with tests that seed a tenant, a company agent, and active/inactive `OrgMember` rows. Assert that search returns only active same-tenant members and omits phone numbers from returned dictionaries.

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
python3 -m pytest backend/tests/test_contact_discovery_service.py -q
```

Expected: fails because `contact_discovery_service` does not exist.

- [ ] **Step 3: Implement `search_contacts` service**

Create a service method:

```python
async def search_contacts(db: AsyncSession, agent: Agent, query: str, channel: str | None = None, limit: int = 10) -> list[dict]:
    ...
```

The method must tenant-scope by `agent.tenant_id`, filter `OrgMember.status == "active"`, search name/transliteration/email/title/department path, filter by provider type when `channel` is set, and return no phone field.

- [ ] **Step 4: Run test and verify GREEN**

Run:

```bash
python3 -m pytest backend/tests/test_contact_discovery_service.py -q
```

Expected: tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add backend/app/services/contact_discovery_service.py backend/tests/test_contact_discovery_service.py
git commit -m "feat: add contact discovery service"
```

## Task 3: Agent Builtin Contact Tools

**Files:**
- Modify: `backend/app/services/agent_tools.py`
- Modify: `backend/tests/test_contact_discovery_service.py`

- [ ] **Step 1: Write failing tests for idempotent add contact**

Add tests for an `add_contact_for_agent` service function that creates one `AgentRelationship`, returns `already_exists` on duplicate calls, rejects cross-tenant contact IDs, and requires non-empty reason.

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
python3 -m pytest backend/tests/test_contact_discovery_service.py -q
```

Expected: fails because `add_contact_for_agent` does not exist.

- [ ] **Step 3: Implement `add_contact_for_agent`**

Add the function to `contact_discovery_service.py`, using `AgentRelationship` with `relation`, `description`, `created_by_user_id`, and `updated_by_user_id`.

- [ ] **Step 4: Register `search_contacts` and `add_contact` in agent tools**

Add tool schemas near `send_channel_message`, then route tool execution to the new service functions.

- [ ] **Step 5: Run tool and service tests**

Run:

```bash
python3 -m pytest backend/tests/test_contact_discovery_service.py backend/tests/test_org_sync_adapter.py -q
```

Expected: tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add backend/app/services/contact_discovery_service.py backend/app/services/agent_tools.py backend/tests/test_contact_discovery_service.py
git commit -m "feat: add agent contact tools"
```

## Task 4: Gateway Parity for OpenClaw Agents

**Files:**
- Modify: `backend/app/api/gateway.py`
- Modify: `backend/app/schemas/schemas.py`
- Create or modify: `backend/tests/test_gateway_contacts.py`

- [ ] **Step 1: Write failing gateway tests**

Test `/api/gateway/search-contacts` and `/api/gateway/add-contact` with an OpenClaw agent API key, verifying tenant scoping and idempotent relationship creation.

- [ ] **Step 2: Run gateway tests and verify RED**

Run:

```bash
python3 -m pytest backend/tests/test_gateway_contacts.py -q
```

Expected: fails because routes do not exist.

- [ ] **Step 3: Implement gateway routes**

Add gateway endpoints that call the same service functions as native tools.

- [ ] **Step 4: Run gateway tests and verify GREEN**

Run:

```bash
python3 -m pytest backend/tests/test_gateway_contacts.py -q
```

Expected: tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add backend/app/api/gateway.py backend/app/schemas/schemas.py backend/tests/test_gateway_contacts.py
git commit -m "feat: expose contact tools to gateway agents"
```

## Task 5: Docker 3008 Validation

**Files:**
- No source changes expected.

- [ ] **Step 1: Identify running containers**

Run:

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
```

Expected: find the local Clawith stack serving port 3008.

- [ ] **Step 2: Run backend tests inside the active backend container**

Run the equivalent of:

```bash
docker exec <backend-container> python -m pytest backend/tests/test_org_sync_adapter.py backend/tests/test_contact_discovery_service.py -q
```

Expected: tests pass.

- [ ] **Step 3: Smoke test UI build or live 3008 page**

Use either:

```bash
cd frontend && npm run build
```

or browser verification against `http://localhost:3008` if the stack mounts this worktree.

Expected: DingTalk provider page shows sync-only copy and SSO login remains a separate toggle.
