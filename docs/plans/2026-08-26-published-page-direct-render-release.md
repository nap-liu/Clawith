# Published Page Direct Rendering — Invalidated Release Record

> Prepared: 2026-08-26
> Released candidate: `v1.10.3-77d5fbc`
> Status: invalidated by the watermark regression

This file records an already executed, now-invalid release decision. It must
not be reused as production release authority.

The release incorrectly removed the platform-owned published-page viewer and
automatic watermarks. That removal was outside the user's authorization. The
user explicitly declined rollback, so remediation is a minimal forward fix.

The authoritative corrective iteration is
`2026-08-26-published-page-watermark-restoration.md`. A new exact-SHA release
plan must be produced only after corrective Docker/browser validation and a
neutral P0/P1/P2-zero change audit. No production action is authorized by this
record.

The invalidated release did not contain a database migration, schema change,
seed change, `publish_page.description` change, or AgentData format change.
