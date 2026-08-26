# Published Page Watermark Restoration — Corrective Iteration

> Created: 2026-08-26
> Base application release: `v1.10.3-77d5fbc`
> Status: implemented, locally validated, and neutral-audited PASS

## Corrected product boundary

The earlier iteration incorrectly treated the platform-owned published-page
viewer and watermark as browser security restrictions. That decision was not
authorized and is superseded by this corrective iteration.

- Keep the platform-owned viewer as the presentation layer for every published
  page.
- `public` pages automatically show an anonymous visitor identifier and access
  time watermark.
- `authenticated` and `restricted` pages automatically show the signed-in
  reader identity watermark.
- Do not place a `sandbox` attribute on the report iframe.
- Do not add route-owned CSP, XFO, COOP, COEP, CORP, Permissions-Policy, or
  `X-Content-Type-Options` restrictions to the report response.
- Preserve `public`, `authenticated`, and `restricted` authorization, live
  revocation, visitor accounting, and tenant isolation.
- Preserve the complete published-page SDK, including OAuth, `ready`,
  `onReady`, `triggerHook`, and report-authored watermarking.

The automatic watermark is a platform presentation and traceability capability,
not an anti-tamper security boundary. Because the authorized report intentionally
runs as unsandboxed same-origin code, a deliberately hostile report can alter
same-origin presentation. This accepted limitation must not be "fixed" by
reintroducing the browser restrictions that broke valid reports.

## Minimal implementation

1. Restore the React published-page viewer route and its full-viewport iframe.
2. Return the viewer shell only after the backend authorizes `/p/<short_id>`.
3. Return stored report bytes through the authorized `__report_embed=1` and
   compatibility content paths without platform browser restrictions.
4. Restore viewer-context watermark data for anonymous and authenticated
   readers.
5. Restore the SDK parent OAuth bridge required by the viewer without restoring
   iframe sandboxing.
6. Restore the publication result's automatic-watermark statement. Keep the
   seeded and fallback `publish_page.description` unchanged.

## Acceptance matrix

| Area | Required evidence |
|---|---|
| Viewer | `/p/<short_id>` renders the platform viewer and one report iframe. |
| Watermark | Public pages show anonymous ID/time; authenticated and restricted pages show reader identity. |
| Report runtime | The iframe has no `sandbox`; same-origin storage, cookies, scripts, CSS, modules, Worker, fonts, forms, downloads, media, navigation, and APIs work. |
| SDK | OAuth bridge, exchange/retry, `ready`, `onReady`, hook payload, and explicit watermark remain operational. |
| Authorization | All three access modes, tenant isolation, active-user checks, approval changes, and live revocation remain enforced on shell, context, and content requests. |
| Accounting | The viewer shell/context do not count content; the report content request records exactly one view. |
| Scope | No migration, schema, seed, AgentData format, AIO sandbox, or Codex plugin configuration change. |

## Local evidence

- Backend focused Docker suite: 36 published-page and response-policy tests
  passed against an isolated PostgreSQL database; compile check passed.
- Frontend cached Docker environment: complete prebuild suite, published-page
  SDK tests, platform-watermark tests, TypeScript, and Vite production build
  passed without downloading dependencies.
- Local Docker 3008 Browser validation: platform watermark host present and
  visible; report iframe `sandbox` attribute absent; report same-origin self-test
  passed localStorage, cookies, SDK/CSS assets, dynamic import, Worker, fonts,
  forms, download declaration, media, navigation, API health, explicit SDK
  watermark, report CSS, and the original URL hash; browser console contained
  zero warnings/errors.
- The pre-existing local 3008 frontend was restored and returned HTTP 200 after
  validation.
- Local Browser SDK validation: the real unsandboxed iframe completed the
  parent OAuth start bridge, callback/exchange, `ready`, `onReady`, hook call,
  automatic/authenticated/manual watermarking, same-origin localStorage and
  cookies; the browser console contained zero warnings/errors.
- Neutral final change audit: PASS with P0/P1/P2 all zero.

## Release boundary

Production remains unchanged by this corrective iteration until a separately
reviewed release plan is authorized. The corrective release must build backend
and frontend from one exact SHA and deploy them as one pair. There is no
database, migration, seed, or AgentData change.
