# Published Page Direct Rendering — Rejected Iteration Record

> Created: 2026-08-26
> Branch: `fix/published-page-direct-render`
> Baseline: `company/main@26bcd4b5`
> Status: rejected and superseded

## Decision correction

This iteration incorrectly classified the platform-owned published-page viewer
and automatic watermark as browser security restrictions. Removing them was not
authorized and caused the published-page watermark regression.

This document is retained only as a historical incident record. It is not an
implementation, acceptance, release, or rollback authority. All earlier
direct-top-level-render decisions, including removal of the viewer, removal of
automatic watermarks, and removal of the SDK parent bridge, are rejected.

The authoritative corrective task is
`2026-08-26-published-page-watermark-restoration.md`:

- keep the platform viewer for all published pages;
- keep automatic anonymous-ID/time watermarks on public pages;
- keep automatic signed-in identity watermarks on authenticated and restricted
  pages;
- keep the report iframe unsandboxed and do not add platform CSP/XFO/COOP/COEP/
  CORP/Permissions-Policy restrictions;
- preserve the original published-page SDK, authorization, live revocation,
  accounting, and tenant isolation.

## Historical release

The rejected implementation was released as `v1.10.3-77d5fbc`. The user
explicitly declined rollback; correction proceeds as a minimal forward fix.
Production release requires a separately reviewed and authorized release plan.
