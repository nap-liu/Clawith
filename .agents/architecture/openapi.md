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
one immutable tenant, has an independent one-time secret, expiry, enabled state,
scopes (`employees:read`, `auth:login`), rate limit, trusted-user delegation flag,
embedding origins and redirect origins. Secrets are hashed; management lists
never return them. Creation and rotation return the new secret once. Rotation,
configuration changes, disabling and revocation invalidate outstanding system
tokens and unconsumed login links via a credential generation boundary.
Revocation is durable and cannot be undone by re-enabling the application.

Management routes are under `/api/enterprise/openapi/applications`, protected by
the existing administrator dependency and current tenant context. Every list,
update, secret rotation, revocation and audit query is tenant-scoped. The service
derives application ownership from the authenticated user's tenant; request bodies
cannot choose or change it. An optional `tenant_id` query only asserts the current
context. Platform administrators must use the ordinary tenant switch first and
receive no unscoped list or cross-tenant object bypass. The former platform-wide
management routes are removed. Existing applications and credentials retain their
stored tenant and need no data migration.

The UI uses the shared Drawer, SettingsForm, inputs, switches, buttons and dialog
owners. Tenant switches discard drafts, one-time secrets and audit views; the
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
| POST `/digital-employees/{id}/access` | `user` | Employee display item and `access_url` |
| POST `/auth/links` | `user`, `redirect_uri`, optional `embed_origin` | `login_url`, `expires_in` |
| POST `/auth/link-exchange` | One-use `code` | Normal platform login response and bound `redirect_uri` |

Employee display items contain `id`, `name`, `avatar_url`, `description` and
`access_url`. Visibility and access use existing employee permission owners.
The employee resource owns the access URL. The generic login service treats its
target as a validated URL and contains no employee ID, H5 path construction,
chat API allowlist, media bridge or alternative turn loop. Other resource types
can use the same login-link service without adding login branches.

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
Optional embedding origins must be explicitly allowed on the client. This
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

Migrations create only system applications, client-scoped subject bindings and
short-lived credential records; normal identity, RBAC, chat and turn tables keep
their existing ownership. Cleanup must preserve unmerged work and the user's
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
