# Engineering handbook

This directory is the complete repository-owned handbook for coding agents and
human maintainers. It contains durable rules and workflows only; current runtime
facts must be inspected rather than remembered.

## Canonical document map

| Need | Canonical source |
|---|---|
| Agent entry point and hard defaults | `AGENTS.md` |
| System-wide architecture | `ARCHITECTURE_SPEC_EN.md` |
| Task-to-document routing | `.agents/workflows/read_architecture.md` |
| Standard diagnose/change/verify lifecycle | `.agents/workflows/engineering_change.md` |
| Design, security, testing, Git, deploy, and release policy | `.agents/rules/` |
| Conversation, context, tools, sandbox, and environment boundaries | `.agents/architecture/` |
| Identity, multi-SSO, SCIM directories, organization graphs, and channel bindings | `.agents/architecture/identity-directory-and-channel-bindings.md` |
| Agent company/department/person grants, additive access, and migration | `.agents/architecture/agent-permissions.md` |
| Executable production release and rollback procedure | `.agents/runbooks/production_release.md` |

The external-memory migration record is historical evidence, not an instruction
layer. Nothing in the table above depends on it.

## Document roles

- **Rules** say what must or must not happen and win over summaries.
- **Architecture** records stable boundaries, invariants, and known traps.
- **Workflows** define the order in which rules and evidence are applied.
- **Runbooks** are executable operational procedures with explicit authority
  gates and rollback points.
- **Historical records** explain provenance only and are never used as a source
  for credentials, current versions, topology, or release state.

## Maintenance rules

When work establishes a durable lesson:

1. verify it against current code and local Docker behavior;
2. update the smallest canonical rule, architecture document, or workflow;
3. update routing when a new document is introduced;
4. remove contradictory or duplicated instructions in the same change;
5. keep incidents, dated releases, test totals, hosts, credentials, and local
   filesystem paths out of normative documentation.

Do not create hidden project knowledge in personal agent memory. If information
is important for future repository work, it belongs in this handbook, code,
tests, migrations, or an approved operations inventory.

System OpenAPI and OAuth client management: [architecture/openapi.md](architecture/openapi.md).
