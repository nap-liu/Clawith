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

## Visitor history

`GET /api/pages/{page_id}/visitors` checks page management permission before
searching. Its optional `q` applies case-insensitive literal substring matching
to visitor display names, emails, and the displayed anonymous visitor label/ID.
Whitespace-only input is unfiltered; SQL wildcard characters remain literal.
Filtering precedes counting and pagination across authenticated and anonymous
visitors, retaining the latest-visit ordering and page scope.

The visitor panel reuses shared search, button and pagination controls. Search
applies on Enter or the search button; reset/clear restores the full history,
and a new query returns to page one. Names reserve at least ten Chinese
characters when truncation is necessary. Email/anonymous status follows the
name with a small gap; short names do not reserve an empty column. Email wraps
to the next line when necessary and truncates within its available width. Truncated names
retain the full native title tooltip, following existing text presentation.
Visitor-panel copy uses standard i18n resources.

## Login recovery

Agents can use the opt-in `create_published_page_login_link` tool to open a
report as the current human participant. It reuses the temporary login owner;
issuance does not check report visibility. The internal signature lasts five
minutes and redeems once into a one-hour login. Reopening with that signature's
own valid token preserves its expiry; report-cookie refresh cannot extend it.
Failed internal exchanges stay on ordinary login without automatic SSO or a
special error flow. See [OpenAPI login lifecycle](openapi.md#internal-report-access-tool).

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
