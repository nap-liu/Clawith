# H5 current host context: implementation design

Status: implemented and independently reviewed. Deployment availability is reported by capabilities.

## Outcome

The existing H5 composer requests the current authorized host snapshot once per
user send, then submits the original message plus JSON through the existing
WebSocket. No iframe reload, automatic question, new conversation service,
context table, polling loop, special OAuth scope or arbitrary payload budget.

## OpenAPI contract

Add `h5_launcher.host_context = {supported: true, version: 1}` to capabilities
only with the implementation. Employee access accepts optional
`host_context: {enabled: true, version: 1}`. Enabled requests require the existing
nonempty `instance_ref` and valid `embed_origin`; existing origin allowlist
semantics remain unchanged. All existing launch fields can coexist.

Access returns optional `host_context: {version: 1, frame_origin: <actual H5 origin>}`.
Derive the origin from the same owner as the generated H5 destination, never the
API request Host header or arbitrary client input. Invalid enabled configuration
returns 400 `invalid_host_context`. No new login endpoint or credential type.

Keep trusted host settings in the existing credential `launcher` JSON. On normal
login exchange, return `host_context: {version: 1, embed_origin, frame_origin,
instance_ref}` alongside the existing redirect/login response. Do not put host
data or snapshots into the ordinary user token or global session preferences.

The login frontend saves this non-secret bootstrap in sessionStorage under a
fresh launch reference and adds only that reference to the H5 destination query
(`host_context=<reference>`). This reference is a browser lookup key, not an auth
credential. H5 reads only the matching key. Distinct iframes in one top-level
origin may share sessionStorage, so never use a global enabled boolean. Normal
H5 URLs without the marker do not enable this mode. Refresh retains the marker;
normal H5 session switching retains this frame's host context mode. Missing
bootstrap for a marked URL fails visibly at send, never silently drops context.

## Browser wire protocol and small optional SDK

Request: `{type:"digital_employee.context.request",version:1,request_id:<message UUID>,attempt_id:<attempt UUID>}`.
Response: `{type:"digital_employee.context.response",version:1,request_id,attempt_id,status:"ready",context:<JSON>}`
or `{...,status:"unavailable",error:{code:"context_unavailable",message:<optional host text>}}`.
Use exact parent window, origin, version and both ID matches. Never `*`.
`attempt_id` changes on each browser retry and only correlates delivery; it never
changes the business snapshot or enters the message fingerprint. Shared in-flight
SDK resolution must reply separately with each waiting attempt's ID.
Browser origin routing is not an extra OAuth caller-domain restriction.

Provide an optional standalone ES module `/sdk/h5-host-context.js` plus TypeScript
declarations. API:

```js
const bridge = createHostContextBridge({
  iframe,
  frameOrigin: access.host_context.frame_origin,
  getContext: ({ requestId, signal }) => getFixedAuthorizedSnapshot(requestId, signal),
});
// Destroy before replacing/hiding the owning iframe or changing its identity.
bridge.dispose();
```

The SDK only owns postMessage filtering, in-flight deduplication, replies and
AbortController cleanup. The host's existing fixed-snapshot owner binds original
page/selection and authorization data to requestId, including failed retries.
The SDK does not query business data, create/resize windows, hold client secrets,
reload iframes or implement a second chat client. SDK users may implement the
documented two-message protocol directly. No npm publication/build framework.

## One send lifecycle

Freeze the existing client message UUID, text, attachments, session and model
selection at send. Before optimistic user-row insertion or clearing the composer,
ask the parent for context. Recheck current frame/session/connection and normal
send permissions after the asynchronous wait. Cancel on session/identity change,
unmount or the existing page-suspension lifecycle; ignore duplicate/late replies.

All actual user questions through the common dispatcher use the same path,
including quick actions and attachment-bearing sends. Session-control commands,
STOP and confirmation/control frames retain existing behavior. Context waiting
does not change the active generation state or stop the preceding answer.

Keep pending context requests separate from an in-memory outbox of sent messages.
STOP cancels unsent context requests before sending its ordinary abort frame.
Edits, suspension and session switches may cancel only unsent work; a sent message
without a durable receipt has an unknown result and retains its UUID/payload for
same-session reconciliation and retry. Never overwrite one unacknowledged message
with a later send. Context failure/timeout leaves
input and files intact and uses the existing chat error area; the original send
button retries the pending action with its existing ID. Editing text/attachments
or changing session cancels it and the next send is a new action. Once context is
ready, transport retries reuse it and the UUID until a durable receipt/history
acknowledges the message. A reconnect must not fetch new page data for that send.
Clear the draft only at the normal successful transport-admission step and only
if it still matches the frozen draft; do not erase edits made during the wait.

An enabled launch suppresses automatic onboarding and fixed initial greetings
for that connection. Existing explicit interaction first questions are already
persisted and execute normally; subsequent composer sends acquire host snapshots.
Other H5 connections keep their original onboarding and greeting behavior.

## Shared message handling

The normal WebSocket user frame gains optional `external_context: <JSON>`. It is
user-level data, not arbitrary trusted metadata. Both idle and in-turn admission
pass it to the same ingest owner and preserve current attachment/scene behavior.
Persist it in `ChatMessage.message_meta.external_context` with the existing
display content. Reuse shared model-history projection and context UI. Initial
execution, durable recovery, in-turn input and OpenClaw gateway delivery must all
receive the same user reference JSON; never rely on history loading alone if a
transport builds its first provider call directly from the incoming frame.

Reuse the current conversation/user/client-event dedup key. For context-bearing
messages, add a canonical payload fingerprint of question, attachment descriptors
and context to metadata. Compare when incoming OR existing metadata contains the
context field, including JSON null. At initial duplicate admission, locked
duplicate admission and unique-key conflict readback, reject
different content as `message_conflict`; identical duplicates return the original
receipt and do not dispatch again. No new dedup table. Preserve ordinary legacy
message behavior when context is absent. Do not interpret context as metadata,
scene configuration, authorization or system instructions.

Host errors are rendered with local i18n; untrusted error text is not HTML.
Timeout is a browser response-wait deadline, not a model execution time limit.
Use the frontend's established request timeout where an applicable owner exists;
otherwise one local transport timeout constant, no settings UI or retry service.

## Scope and acceptance

Primary: OpenAPI/login bootstrap, documentation, integration, 3008 delivery.
H5 implementation: shared context bridge hook, composer/retry lifecycle, optional
host SDK, login bootstrap module, locales. Backend implementation: WS adapters,
shared ingest fingerprint/context projection and connection greeting opt-out.
Independent reviewer: design first, then actual diff and bounded Docker/browser
acceptance, with no implementation ownership.

Accept one real host iframe flow with authorized fixture data: empty open has no
automatic message; first send gets selection A; next send after host changes gets
B; history retains A; scene snapshot still applies. Cover concurrent frames,
timeout and same-ID retry, reconnect without duplicate send, in-turn admission,
attachments, and old H5/interaction paths. Reuse existing checks; add no test
framework or repetitive test files. Every changed source remains <=800 lines.

## Independent design audit

The neutral reviewer confirmed the normalized approach and required the split
between unsent context waiting and sent-but-unacknowledged outbox entries,
symmetric duplicate comparison on every return path, and per-attempt browser
correlation. These are included above. The reviewer also confirmed no new
credential type, context service or cross-origin sessionStorage promise is needed:
the existing login page and its relative H5 destination share an origin.

The implementation audit additionally requires definitive admission rejections
to correlate the existing `rejected_message_id`, restore untouched drafts and
leave automatic resend. STOP suppresses automatic replay of current-session
unknown sends while retaining history reconciliation. `/continue` remains an
ordinary control command and never enters the host snapshot outbox.

## Verification evidence

- Independent implementation review passed after the three send/control fixes.
- Isolated Docker PostgreSQL checks exercised OAuth/access/exchange/bootstrap,
  concurrent receipt identity, conflict detection, JSON null/false and a large
  snapshot, attachments, history IDs, native initial input and gateway delivery.
- Existing WebSocket initialization and inbox behavioral checks passed (23 cases).
- Docker TypeScript and Vite build passed; nginx configuration and SDK JavaScript
  MIME, cross-origin header and revalidation policy passed against the built app.
