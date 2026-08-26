# Published Page Direct Rendering — Immutable Iteration Task

> Created: 2026-08-26
> Branch: `fix/published-page-direct-render`
> Baseline: `company/main@26bcd4b5`
> Status: implemented and locally validated
> Neutral plan audit: PASS — no unresolved P0, P1, or implementation blocker
> Neutral implementation audit: PASS — no unresolved P0, P1, or P2

## Product decision and trust boundary

- The three existing access modes remain the only platform authorization boundary for published reports: `public`, `authenticated`, and `restricted`.
- After authorization succeeds, `/p/<short_id>` returns the stored report as an ordinary top-level, same-origin HTML document. The platform does not constrain its scripts, storage, navigation, network access, embedding, or browser capabilities.
- A person allowed to publish HTML is therefore a trusted same-origin code author. Their report can read non-HttpOnly platform storage, call same-origin APIs with the reader's credentials, navigate the page, and transmit information. The access modes decide who may obtain the document; they do not reduce the document's authority after delivery. This risk is explicitly accepted for this iteration.
- The platform-owned mandatory watermark is retired. Access counts and named/anonymous visitor audit remain. Report authors may still opt into the published-page SDK's `data-watermark` behavior.
- The platform does not remove or override restrictions authored inside the report itself, including a report-owned `<meta http-equiv="Content-Security-Policy">`.

## Frozen workstreams

1. [x] **One authoritative authorization-and-render path** — Reuse the current tenant-aware `can_view_page` policy for `/p/<short_id>` and `/api/pages/<short_id>/content`; re-check live user and access state on every request; remove all viewer-only branches and bypass-prone alternate behavior.

2. [x] **Byte-preserving direct response** — Read report content with `read_bytes`; return those bytes unchanged with explicit `Content-Type: text/html` without a forced charset and `Cache-Control: no-store`; retain only required cookies plus ordinary protocol, trace, CORS-middleware, and gateway headers.

3. [x] **Retire platform execution and embedding restrictions** — Remove the viewer iframe, sandbox, `X-Accel-Redirect`, `__report_embed`, viewer context, fetch-metadata assumptions, internal nginx viewer location, and route-owned CSP, XFO, COOP, COEP, CORP, Permissions-Policy, and `X-Content-Type-Options` behavior.

4. [x] **Preserve access and audit lifecycle** — Keep the page session, HttpOnly cookie, access UI, request/approval workflow, tenant isolation, real-time revocation, public visitor cookie, named/anonymous aggregation, and exactly-once view accounting for each canonical content request.

5. [x] **Preserve the complete published-page SDK** — Remove the obsolete parent-viewer `postMessage` wait path and let the current browsing context initiate OAuth. Preserve short-ID detection, `ready`, `onReady`, OAuth start/callback/exchange, query cleanup, retry after failed exchange, `triggerHook`, and explicit `data-watermark`.

6. [x] **Align Agent-visible publication contracts** — Remove the automatic-watermark promise from the runtime fallback definition, database seeder source of truth, and publication result text. Verify the seeded database row and actual `get_agent_tools_for_llm()` output in Docker.

7. [x] **Preserve the external HTTPS scheme** — In the authoritative nginx template, retain a trusted inbound `X-Forwarded-Proto` and fall back to `$scheme` only when absent. In the backend, accept only `http` or `https` before using the value for access return URLs or Secure cookies.

8. [x] **Observable Docker regression** — Use an isolated PostgreSQL database and Docker-only build/test/browser execution. Cover the access matrix, original response bytes, absent route-owned restriction headers, browser-native report capabilities, SDK behavior, external public embedding, HTTPS proxy behavior, counts, and actual seeded tool visibility.

9. [x] **Coordinated release and rollback contract** — No database migration is required. A future authorized release must deploy new backend before new frontend. Rollback must restore old frontend before old backend. Backend and frontend must use the same release SHA; no database restore is required for this change.

The workstream count, direction, trust decision, SDK guarantee, and acceptance boundaries above are frozen. Any change requires explicit user approval and an appended decision record.

## Acceptance matrix

| Area | Required observable evidence |
|---|---|
| Authorization | Public anonymous access succeeds. Authenticated access requires an active same-tenant page session. Restricted access permits only the publisher, Agent creator, company/platform administrators, and approved users. Cross-tenant, unapproved, inactive, expired, and forged sessions fail. |
| Live revocation | Changing access mode or approval state affects the next `/p` and `/content` request without relying on cached viewer state. |
| Original response | Response body is byte-for-byte equal to stored content. No injection, cleanup, decoding, or re-encoding occurs. Report-authored meta elements remain unchanged. |
| Cache and headers | `/p` and `/content` are `no-store`. The page route does not add CSP, XFO, COOP, COEP, CORP, Permissions-Policy, or `X-Content-Type-Options`. Generic trace/protocol/CORS middleware headers are allowed. |
| Top-level capabilities | `window === top`, origin is not `null`, and localStorage, ordinary cookies, same-origin API calls, ES modules, dynamic import, CSS, fonts, Worker, forms, downloads, popups, media, and navigation operate under normal browser rules. |
| Browser console | The test report has no platform-caused sandbox, storage, cookie, CORS, or CSP error. |
| SDK top-level | A real browser observes the auth-start navigation, callback and exchange, `user`, `onReady`, code/state cleanup, hook POST body and response, automatic explicit watermark, and exchange retry. |
| Public external iframe | A second-origin host renders the public report and supports `triggerHook` and `watermark({text|user})`. OAuth-dependent SDK behavior completes with a frame-compatible test IdP in the current frame and never enters a platform-created permanent wait. |
| Protected external iframe | Not guaranteed in this iteration because third-party page-session cookies and real IdP framing policies are browser/provider boundaries. The canonical top-level `/p/<short_id>` remains the supported path. |
| HTTPS | A trusted outer `https` proxy produces an `https://` return URL and Secure page-session cookie. Direct local HTTP remains functional. Invalid forwarded protocols are ignored. |
| Counts and audit | Each canonical report response records one view. Named and anonymous aggregation plus management totals remain correct. |
| Tool contract | Docker seeding and actual LLM tool output no longer claim an automatic platform watermark. |
| Validation target | Backend, frontend, proxy, and browser evidence come from one source SHA in local Docker. Production is not used for development validation. |

## External embedding and SDK boundary

- Public report rendering, `triggerHook`, and explicit `watermark({text|user})` are guaranteed in cross-site iframes.
- OAuth-dependent `ready`, `onReady`, and automatic `data-watermark` run in the current browsing context. The mechanical iframe flow is tested with a frame-compatible identity provider.
- A real identity provider may reject iframe navigation through its own CSP/XFO, and a browser may restrict third-party cookies. The platform does not bypass those external policies. In that case the embedding host must open the canonical report URL as a top-level document.

## Release and rollback boundary

Implementation and validation are local only in this iteration. Production mutation requires separate approval and the canonical release runbook.

- Cutover order: new backend, then new frontend.
- Rollback order: old frontend, then old backend.
- Both images use one release SHA.
- No schema, business-data, or AgentData rollback is needed. The backend's
  normal idempotent builtin-tool seeding may reconcile the `publish_page`
  description, but it does not require a PostgreSQL or AgentData backup for
  this release.

## Local completion evidence

- Neutral plan audit: PASS before implementation. Neutral final implementation audit: PASS with no P0, P1, or P2.
- Backend: 36 focused tests passed in Docker against isolated PostgreSQL, including live access-mode/approval revocation, same-tenant administrator access, cross-tenant administrator denial, inactive users, forged and expired sessions, response bytes/headers, visit aggregation, HTTPS forwarding, publication output, database seeding, and actual LLM tool visibility.
- Backend full suite: 2,400 tests passed with 28 skipped in one isolated-Docker run; the suite's single order-dependent MCP catalog test passed separately after rebuilding the isolated database, for 2,401/2,401 executed tests passing. No MCP code was changed.
- Frontend: the production Docker image completed the full existing `prebuild`, the added SDK runtime test, TypeScript compilation, and Vite build; the generated nginx configuration passed `nginx -t`.
- SDK browser: real Docker Chromium passed top-level and cross-origin iframe OAuth start/callback/exchange, exchange retry, `Clawith.user`, `onReady`, query cleanup, short-ID hook payload, `triggerHook`, automatic and explicit watermarking, localStorage, and current-frame navigation.
- Direct-render browser: the exact candidate backend/frontend stack was run through local port 3008 and passed both top-level and cross-origin iframe rendering with a non-null origin, same-origin SDK/CSS/API access, localStorage, ordinary top-level cookies, dynamic import, Worker, fonts, forms, a completed user-gesture download, user-gesture audio playback, navigation, a user-gesture popup, report CSS, and explicit watermarking. CDP runtime, console, and browser logs contained no errors. The pre-existing 3008 frontend container was restored and rechecked after the isolated candidate stack was removed.
- Candidate scope: the branch remains directly based on `company/main@26bcd4b5`; no Alembic migration, database schema, AgentData, sandbox image, or release-workflow file is changed.
- No production deployment, production data mutation, or schema migration was performed.
