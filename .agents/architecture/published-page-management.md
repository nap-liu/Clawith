# Published page management

## Management list contract

`GET /api/pages/mine` is the tenant-scoped management inventory for published
pages.  Its authorization predicate is always applied before filters.  Platform
and organization administrators can manage pages in their tenant; other users
see only pages owned by them or pages belonging to Agents they created.  Search
and filter parameters may narrow this set but must never broaden it.

The list supports these composable filters:

- `agent_ids`: exact Agent identifiers, with legacy singular `agent_id`
  retained for compatibility;
- `access_mode`: one exact value from `public`, `authenticated`, or
  `restricted`; invalid values fail request validation;
- `q`: a case-insensitive fuzzy match over the public address identifier,
  source path, title, original publisher display name, and most recent
  publisher/modifier display name.

For address search, both the short public identifier and a pasted full
`/p/{short_id}` URL must match.  Query wildcard characters are treated as
literal user input, not SQL pattern syntax.  Empty or whitespace-only search
input does not add a predicate.  Pagination totals are calculated from the
same joins and predicates as the returned rows.

The management UI keeps applied filters in URL search parameters so refresh,
browser navigation, and shared internal links reproduce the same list.  The
authorization selector is single-choice because the API contract is an exact
filter.  Search copy names the supported address, title, publisher, and editor
fields.  When one or more Agents are selected, the multi-select exposes a
one-click clear action without requiring the menu to be opened; clearing also
removes `agent_ids` from the URL and resets pagination.  All controls reuse
shared dropdown, multi-select, search, button, and i18n primitives.

## Login recovery

Protected report URLs request automatic SSO when they redirect to the access
page, carrying the report's tenant and original return URL. An optional `sso`
query selects the provider; otherwise the existing enabled-provider order
applies. Without an available SSO provider, the ordinary login page remains.
Public reports and already authorized visits do not start SSO.
The report viewer skips the application's platform-token bootstrap; its
existing cookie and report API checks own authorization and session recovery.
An expired platform token must not interrupt a valid report session or a
public report.

Missing credentials and API 401 responses share the frontend login URL builder.
On the report access page it carries `tenant_id`, `auto_login`, `sso`, and the
report return URL directly onto the login URL; a 401 must not bury this context
inside another `return_to`. Login-page recovery preserves its existing query.
Ordinary application routes retain their existing login behavior. Report
access checks and restrictions still apply after authentication.

## Validation

API tests must assert observable results for every supported fuzzy field, a
pasted full public URL, exact authorization modes, invalid-mode validation,
combined Agent filtering, tenant isolation, and management authorization.
Frontend validation must cover URL parameter synchronization, reset behavior,
responsive layout, and the request emitted by the management page.  Backend,
frontend build, and browser validation run only in the isolated local Docker
environment.
