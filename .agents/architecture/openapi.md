# Standard OpenAPI system integrations

The platform exposes one system-to-system service boundary. External systems
are OAuth clients. They do not share databases, JWT signing keys, internal user
models, employee permissions, or conversation implementations with this server.
Read this document for OpenAPI client management, authentication, SDK consumers,
delegated-user discovery and temporary login links.

## Standards and management

- OAuth 2.0 Client Credentials follows RFC 6749 sections 2.3.1 and 4.4.
  `POST /api/openapi/v1/auth/token` accepts form-urlencoded `grant_type` and
  optional `scope`, with HTTP Basic client credentials. Client ID and secret
  are individually form-urlencoded before building the Basic value. JSON token
  requests and custom authentication headers are not supported.
- Bearer access follows RFC 6750. Resources require the Authorization header.
  Tokens are short-lived opaque values, hashed at rest, bound to one client and
  its explicitly granted scopes. Query tokens, refresh tokens, password grants,
  user authorization-code flows and OpenID Connect are outside this interface.
- RFC 7009 revocation is `POST /api/openapi/v1/auth/revoke`: Basic client auth,
  form-urlencoded `token` and optional `token_type_hint`. Unknown, already
  revoked and other-client tokens produce the same successful response. Only
  the authenticated client's token can be changed.
- RFC 8414 metadata is `/.well-known/oauth-authorization-server`. The issuer and
  endpoint URLs come only from the existing trusted platform URL configuration
  owner (environment `PUBLIC_BASE_URL`, then system setting), requiring HTTPS; without a
  valid issuer configuration discovery returns `issuer_not_configured`.
- The published OpenAPI schema declares a `clientCredentials` OAuth security
  scheme and the token endpoint's Basic scheme. Standard OAuth libraries can
  obtain/revoke tokens; generated API clients use the documented business
  request/response schemas. There is no second bespoke client auth protocol.

Company administrators create clients in the enterprise settings OpenAPI tab. A client belongs to
one immutable tenant, has an independent secret, expiry, enabled state,
scopes (`employees:read`, `auth:login`), rate limit, trusted-user delegation flag,
embedding origins and redirect origins. Authentication uses a secret hash;
creation and rotation also store the secret directly for administrator viewing.
Management lists never return the secret or its hash.
The tenant-scoped POST `/{app_id}/credentials` management action returns the
stored credentials to administrators, records a secret-free audit event and returns no-store.
Rotation, configuration changes, disabling and revocation invalidate outstanding system
tokens and unconsumed login links via a credential generation boundary.
Revocation is durable and cannot be undone by re-enabling the application.

Management routes are under `/api/enterprise/openapi/applications`, protected by
the existing administrator dependency and current tenant context. Every list,
update, credential view, secret rotation, revocation and audit query is tenant-scoped. The service
derives application ownership from the authenticated user's tenant; request bodies
cannot choose or change it. An optional `tenant_id` query only asserts the current
context. Platform administrators must use the ordinary tenant switch first and
receive no unscoped list or cross-tenant object bypass. The former platform-wide
management routes are removed. Existing applications and credentials retain their
stored tenant and need no data migration.

The UI uses the shared Drawer, SettingsForm, inputs, switches, buttons and dialog
owners. Tenant switches discard drafts, displayed secrets and audit views; the
application list is keyed by tenant. Management copy, including audit actions,
comes from the standard locale resources.

No external dynamic client registration protocol is exposed. Disabling an external application does not revoke an
ordinary user login already established through the platform's login owner.

## Delegated user is business data

After OAuth authenticates the client, authorized business requests can include:

```json
{"user": {"subject": "opaque-source-user", "phone": "verified-phone", "asserted_at": 0}}
```

`asserted_at` is the current Unix timestamp (60-second clock tolerance), not a
claim that the person just performed an interactive login. Only clients with
trusted-user delegation enabled can submit this context. This is business
identity delegation over OAuth, not a custom authentication header or a separate
signature protocol. The delegating system chooses its authorized user context;
the API does not distinguish its editor, preview or runtime concepts.

The existing canonical identity service normalizes the phone and resolves an
existing active global Identity plus one active membership in the client's
tenant. No account creation, role synchronization or permission grant occurs.
A client-scoped hashed subject binding prevents a later phone assertion from
silently switching an existing subject to another person. Ambiguous or conflicting
matches fail closed. Existing global, membership and tenant active-state checks
remain authoritative.

Tenant-managed clients cannot delegate platform administrators, whether that
authority comes from the tenant role or the global Identity flag. Both identity
resolution and login-code exchange enforce this boundary, including when a user
is promoted after a link was issued. Enterprise self-service must not mint a
platform-administrator login through trusted phone assertions.

## Business API contract

| Method and path under `/api/openapi/v1` | Input | Result |
|---|---|---|
| GET `/capabilities` | Bearer token | Version, granted scopes and delegation availability |
| POST `/digital-employees/search` | `user`, optional `search`, `page`, `page_size` | `items`, `total`, `page`, `page_size`, `has_more` |
| POST `/digital-employees/{id}/access` | `user`, optional `instance_ref`, `interaction`, `embed_origin` | Employee display item; extended requests also return a temporary `login_url` |
| POST `/auth/links` | `user`, `redirect_uri`, optional `embed_origin` | `login_url`, `expires_in` |
| POST `/auth/link-exchange` | One-use `code` | Normal platform login response and bound `redirect_uri` |

Employee display items contain `id`, `name`, `avatar_url`, `description` and
`access_url`. Visibility and access use existing employee permission owners.
The employee resource owns the access URL and optional H5 activation adapter.
The generic login issuer treats its target as a validated URL, stores an optional
opaque activation reference and does not construct employee paths or execute
chat turns. Exchange dispatches that reference to the employee activation owner
after ordinary login validation. Generic login links retain their existing path.

## Generic temporary login lifecycle

`auth:login` and trusted delegation allow a client to request a 60-second signed
single-use login code. The signature has a dedicated audience and binds client,
credential generation, expiry and redirect target. It contains no phone or
client secret. The database stores its hash and consumes it atomically. A code
cannot authenticate a normal REST or WebSocket endpoint.

The returned `/openapi/login?code=...` URL is a business login handoff, not an
OAuth Authorization Code grant. The public login page removes the code from
its address, exchanges it once, invokes normal frontend login state and navigates
to the server-bound target. The existing platform login owner issues the normal
user token/cookie. Existing user, employee and session permission checks then
apply to the destination, independently of the external client.

Targets must be relative application paths or use the configured public origin
or an application redirect-origin allowlist. Network-path references, control
characters, backslashes and credential-bearing URL authorities are rejected.
An empty embedding allowlist permits any valid embedding origin. A nonempty
list restricts both link issuance and exchange to its configured origins. This
configuration validates the requested embedding intent; it does not introduce
new page-specific frame policies or grant employee access.

The login frontend route suppresses access logs and sends no-referrer/no-store.
Access tokens are returned in POST responses, never in login or destination
URLs. Token responses use no-store/no-cache, and resource errors use standard
Bearer challenges. Audit stores client/user references, operation, status and
request ID, never secrets, request bodies, phone values, code values or raw
Authorization headers. Audit remains in the existing AuditLog domain.

## Validation and lifecycle

Use isolated Docker PostgreSQL and Redis with the exact checkout mounted.
Validate normal create-client → OAuth token → discovery → generic login →
normal resource access before adding focused risk checks. Required risks are
scope reduction, cross-client revocation, replay/concurrent consume, altered or
expired code, disabled/rotated client, inactive account, cross-tenant discovery,
redirect tampering and code rejection by ordinary REST/WS authentication.

Migrations create system applications, client-scoped subject bindings,
short-lived credentials and optional interaction records; normal identity, RBAC,
chat and turn tables keep their existing ownership. Cleanup must preserve unmerged work and the user's
other worktrees and shared local stacks.

OpenAPI models register through `app.models.registry`. The shared bootstrap and
Alembic boundary owns schema creation; neither application lifespan nor a second
entrypoint import list creates these tables. The integration merge revision joins
the OpenAPI migration and bootstrap-index repair without rewriting either parent.

OpenAPI opts into the shared identity owner's exact mainland phone lookup:
domestic 11-digit, `+86`, `86` and `0086` forms resolve to the same existing
membership when unambiguous. Other explicit international prefixes are preserved.
Equivalent duplicate identities fail closed. This is query compatibility only;
no phone rewriting, whole-database migration or other-channel policy change occurs.

## Design principle

Keep integrations simple, efficient and normalized. Use the fewest services and
models required by a real caller. Reuse the existing identity, permission, login,
conversation and shared UI owners; do not build an integration framework or a
parallel authentication, session, renderer or synchronization mechanism.

## Optional context interaction for H5 access

The H5 access extension must stay simple, normalized and compatible. Extend
`POST /digital-employees/{id}/access` with optional `instance_ref`, `interaction`
and `embed_origin`. Requests without these additions retain the existing employee
response and ordinary H5 behavior. Extended access returns the existing employee
fields plus `login_url`, `expires_in` and, for an interaction, `request_id`.
It reuses `employees:read`, `auth:login`, trusted delegation and generic login
issuance; no new endpoint, OAuth grant, scope or separate chat runtime is added.

An opaque instance reference selects the current ordinary ChatSession for the
OAuth application, delegated user, employee and instance. A first ordinary open
creates that instance's session; later opens resume it. A new interaction starts
a new session and updates the instance association only on successful activation.
Sessions remain independently addressable through ordinary history and access
checks. Omitting the instance preserves the ordinary H5 selection behavior.

One interaction record owns the application/user/request-id idempotency key,
business-content fingerprint, pending payload and activated session/message
references. Same-key same-content requests reuse it and may issue another login
link; changed employee, instance, message or context returns 409. The assertion
time and OAuth token are not business content. Issuance never executes the model.
Successful login and current employee authorization atomically activate one
ordinary session and user message, then use the existing durable execution owner.
Repeat activation never resends, including after a failed first answer. Pending
snapshots expire after ten minutes; expired identities remain as tombstones for
at least 24 hours. Activated snapshots follow ordinary conversation retention.

Context is generic JSON. Preserve its structure and value types in message
metadata and the model's user-level reference material, without interpreting
external business models or promoting data into system/developer instructions.
H5 adds an optional collapsible context view using shared presentation components;
its original composer, session selection, streaming, attachments, confirmation,
STOP and recovery remain the authoritative behavior.

Do not add arbitrary message-length, interaction-byte, dataset-row, concurrency,
execution-time, domain-binding or dedicated-scope restrictions for this extension.
Existing application/user/employee authorization, request/model capacity handling,
URL syntax checks and explicit application origin configuration remain in force.
The suggested 8000-character/128-KiB/200-row budgets are not new hard limits.
Capabilities publish support and version through the existing response; no new
configuration center, business-instance table, message queue or polling API.
URLs and ordinary audit contain references and outcomes, never question/context
payloads. Validate actual first-question, isolation and existing H5 behavior with
focused Docker checks; test volume is not a delivery goal.

Native turns use the existing durable resume owner after commit; OpenClaw turns
enter its existing gateway queue in the activation transaction. User-facing
`display_content` remains the question, while the shared LLM history projection
includes `external_context` as user-level reference JSON. Pending-payload cleanup
runs opportunistically during system-token issuance; expiry is enforced on every
interaction access even when no cleanup traffic has occurred.

The external integration contract and runnable requests are maintained in
[`docs/openapi-integration.md`](../../docs/openapi-integration.md); the generated
[`docs/openapi-v1.json`](../../docs/openapi-v1.json) describes the system routes.
