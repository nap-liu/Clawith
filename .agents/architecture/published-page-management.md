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

## Validation

API tests must assert observable results for every supported fuzzy field, a
pasted full public URL, exact authorization modes, invalid-mode validation,
combined Agent filtering, tenant isolation, and management authorization.
Frontend validation must cover URL parameter synchronization, reset behavior,
responsive layout, and the request emitted by the management page.  Backend,
frontend build, and browser validation run only in the isolated local Docker
environment.
