# Agent access grants

`agent_permissions` is the authoritative source of optional Agent access.
Each grant has an Agent, a company/department/user subject and a use/manage
level. Company means active members of the Agent's tenant, never anonymous or
cross-tenant access. Department grants dynamically include active descendants
through the normalized directory graph and memberships; they are not expanded
into stored user grants.

After the existing identity, tenant and resource gates, matching grants combine
at the highest level: none < use < manage. A personal use grant cannot reduce a
department manage grant. Revoking one grant recomputes the remaining sources.
Creator and administrator authority remain existing built-in rules, evaluated
at runtime rather than copied into new grant rows. Historical explicit grants
are retained unless explicitly changed or suppressed by the legacy migration.

`core.permissions.resolve_agent_grant_level` is shared by HTTP and user-ID
authorization. Directory visibility and accessible-member queries use the same
grant semantics in SQL. `Agent.company_grant_level` is a query projection from
the company row. The old `access_mode` and `company_access_level` columns remain
compatibility display projections maintained by the shared writer; they must
never independently grant access. New consumers use grants or their query
projections.

`services.agent_permissions.update_agent_grants` owns normalized writes for
REST creation/settings and MCP access tools. It locks the Agent, rechecks the
operator, validates new/changed subjects within the tenant, updates only changed
rows and appends an AuditLog in the caller's transaction. A full `grants` request
replaces the optional roster; incremental MCP edits preserve unnamed subjects.
Legacy company/custom updates preserve omitted subject lists. Legacy private
explicitly clears optional grants. Company access off does not clear departmental
or personal grants; there are no inactive grants waiting for a mode switch.

All creation and settings surfaces reuse `AgentPermissionsEditor` and
`OrgMemberAccessPicker`. Company access and subject grants are independently
editable. Creator-only is a clear-optional-grants shortcut; administrators retain
their existing governance authority. Human grants do not replace A2A relationship,
project membership, session visibility or execution identity gates.

For human session audit, effective `manage` access is sufficient regardless of
the tenant membership role, including ordinary `member` users. REST history,
read-only WebSocket monitoring and session-introspection tools share this rule;
the frontend exposes the same all-sessions entry. Company, department and user
grants use the same resolved level. Existing project-specific gates remain
separate from this role correction. This read capability does not change the
existing authority to rename/delete another user's Session, send through MCP,
or recall messages. Those operations select the existing write scope; read-only
WebSocket monitoring still cannot send as the Session owner.

Settings build editable subjects from the complete `grants` response. Display
rosters only supply names and directory metadata; built-in administrator labels
must not erase an explicit grant or replace its stored level. Inactive and
unresolved subjects remain in the roster until explicitly removed. A company
toggle preserves every other grant. Plaza is deprecated and excluded from the
supported grant-consumer consolidation scope.

## Migration boundary

`additive_agent_permissions` archives each previous roster in AuditLog, removes
grants ignored by the previous access mode, and materializes company permission
from the previous effective company level. This avoids activating stale hidden
grants. Private grants are removed because the prior evaluator honored only
built-in authority there. Custom company rows are removed; custom user and
department grants remain.
Its parent is the company main migration `model_extra_headers`; the migration
graph has one head. Re-parenting this unpublished candidate requires a new
isolated validation database rather than reusing an earlier candidate's stamp.

This is a one-time **offline writer cutover**, not a backward-compatible online
migration. The default online production workflow is NO-GO. Before touching an
existing database, separately approve a maintenance procedure, block new work,
drain active work, stop every old API/worker/connector and other permission writer,
and verify no old instance can restart. Snapshot the quiescent PostgreSQL state.
Only then run the candidate image with its entrypoint overridden:

```sh
alembic -x agent_permissions_cutover=offline upgrade heads
```

The x-argument is an operator assertion of that verified maintenance state, not
automatic detection or a writer fence. Without it the migration fails before
data writes, including programmatic bootstrap on an existing parent database.
Exclusive locks on Agents and grants use a 5-second lock timeout; statements
are bounded to 60 seconds. These locks cover conversion only, not the period
after commit. Keep old writers stopped after migration and start only the
candidate release. A new empty database still follows normal create-and-stamp
bootstrap, which has no legacy grants to convert.

Archived rosters are audit evidence only. Downgrade refuses whenever Agents or
normalization audit records exist; only an empty database may downgrade. This
does not rely on new-writer audit timestamps, which cannot detect old REST edits.
Do not restore an old roster over current decisions or directly switch populated
state back to the old evaluator. After successful migration, recovery requires a
compatible forward fix or a separately approved, rehearsed pre-cutover database
restore with its loss-of-new-writes decision. If migration rolls back entirely,
verify the parent revision and original grants before resuming the old release.

Validate changes using isolated PostgreSQL and Docker: company plus personal and
department management, revocation fallbacks, inactive membership, tenant rejection,
REST/MCP parity, concurrent writes, old-data migration and actual browser edits.
Include default online/bootstrap rejection, bounded lock failure, old REST
roster changes followed by downgrade refusal, and UI saves preserving explicit
administrator and inactive-department grants.
