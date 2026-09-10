# Model reasoning controls

## Product contract

The platform exposes one provider-neutral reasoning control everywhere a model
or imagination override can be selected.  The canonical value is
`reasoning_effort`:

```text
null | none | minimal | low | medium | high | xhigh | max
```

`null` means inherit and is never equivalent to `none`.  `none` is an explicit
request to disable reasoning for lower latency.  The other values form an
ordered scale; product APIs, database rows, tool schemas, and UI state must not
expose provider token budgets or provider-specific names.

The product label for `null` is **Auto** (`自动`).  At a model configuration
boundary this omits the provider parameter and uses the model/provider default
reasoning policy.  This is not a universal adaptive on/off switch: a
reasoning-only model still always reasons, while the model dynamically decides
the actual reasoning length within its default effort or budget.  At a higher
boundary (turn, project member, agent, or background resource), Auto first
inherits any lower configured value; only when every layer is unset does it
become the provider default.  The label must never imply that an existing lower
explicit setting is bypassed or that every model can decide whether to reason.

The Web composer places a compact reasoning selector beside the model selector.
It mirrors the existing temporary model picker: the browser keeps the selection
for subsequent turns while that Agent page is active, and the backend snapshots
it onto each accepted user turn.  Agent settings supply the default.  Tasks,
schedules, triggers, Subagent runs, and frozen project-member runtime settings
expose the same optional override anywhere they already expose `model_id` and
`temperature`.

All visible labels, help text, errors, and accessibility names use the shared
i18n resources.  The selector uses the existing compact dropdown interaction
and design tokens; it must not introduce a page-local slider or a second visual
language.

## Resolution and durability

The effective value is resolved once, before provider I/O, with this precedence:

```text
accepted-turn or execution override
  > frozen project-member override
  > Agent override
  > selected model default
  > null (provider default)
```

The first non-null value wins, including `none`.  Model failover retains the
resolved product intent but adapts it independently for the fallback model.
Long-running work receives the effective value in its immutable
`RuntimeLLMModel`; it must not retain ORM rows or re-read mutable settings after
dispatch.

For Web chat, the accepted user message metadata is the durable turn snapshot.
It contains `model_id` and `reasoning_effort` when selected.  Recovery reads
these values from that exact anchor, not from mutable browser or Session state.
Session `im_config` keeps IM command selections for the next turn, but is not
the authority for an already accepted turn.  Web composer state remains
temporary client state, matching the existing temporary model selector.

External IM channels expose the same session-scoped behavior through
`/reasoning <none|minimal|low|medium|high|xhigh|max>`.  `/reasoning status`
reports the effective value and its source; `/reasoning auto` (`default` and
`reset` remain compatible aliases) removes only the current Session override.
This is intentionally separate from
the existing `/thinking on|off` command, which controls whether reasoning text
is displayed in IM and does not change model computation.  Model changes and
reasoning changes share the Session row lock, and the ingestion path snapshots
both settings onto the subsequently accepted message.

PATCH APIs distinguish an omitted field from explicit `null`: omission leaves
the stored override unchanged; `null` clears it back to inheritance.  This rule
also applies to model and imagination defaults and prevents UI reset actions
from becoming no-ops.

## Protocol normalization

Provider adaptation is centralized in the LLM transport layer.  Callers pass
only the canonical effort.  The adapter derives a capability profile from the
selected protocol, endpoint, and exact model identifier, then emits only the
provider's supported request shape.

Profiles have these behavioral forms:

- `native_effort`: translate the level to the provider effort enum.
- `budget`: translate the level to a bounded provider thinking-token budget.
- `toggle`: translate `none` versus any enabled level; collapse enabled levels.
- `always_on`: collapse the requested levels to supported effort/budget values;
  reject `none` because the provider cannot truthfully disable reasoning.
- `fixed`: accept inheritance only; reject explicit values.
- `unsupported`: reject explicit values without changing an ordinary inherited
  request.

Known OpenAI-compatible model families are matched before generic provider
fallbacks.  DashScope-hosted third-party models therefore use their model-family
contract rather than the configured display-provider string.  Aliases are
matched only when the provider documents stable semantics.  Unknown model names
must not be guessed into a budget profile.

The initial normalization table is:

| Family | Profile | Canonical mapping |
|---|---|---|
| OpenAI GPT-5.6 | native effort | all seven values |
| Qwen 3.8 Max/Plus/Flash | native effort | `none` disables; minimal/low -> low; medium -> medium; high/xhigh/max -> xhigh |
| Qwen 3.8 2.4T A95B on the production endpoint | always-on effort | enabled levels use the Qwen 3.8 map; `none` is rejected |
| Qwen 3.5/3.6/3.7 Plus or Flash | budget | `none` disables; enabled levels -> 1024/4096/16384/32768/65536/model cap |
| Qwen 3.7 Max current alias, 2026-05-20, 2026-06-08 | budget | `none` disables; enabled levels use the budget scale |
| Qwen 3.7 Max preview / 2026-05-17 | always-on budget | enabled levels use the budget scale; `none` is rejected |
| GLM 5.2 | native effort | all seven values |
| GLM 5/5.1 | native effort | `max` -> `xhigh`; other values unchanged |
| ZHIPU GLM 5.3 | always-on effort | minimal/low -> low; medium/high -> high; xhigh/max -> max |
| DeepSeek V4 | native/toggle | `none` disables; minimal/low/medium/high -> high; xhigh/max -> max |
| Kimi K2.5/K2.6 | budget | `none` disables; enabled levels use the budget scale |
| Kimi K2.7 Code | always-on budget | enabled levels use the budget scale; `none` is rejected |
| Kimi K3 | always-on effort | native low/high/max; platform maps minimal/low -> low, medium/high -> high, xhigh/max -> max |
| MiMo | toggle | `none` disables; every enabled level enables thinking |
| MiniMax M1/M2.5 | fixed | explicit values are rejected; M1 is retained only as a retired/unavailable legacy configuration |
| Bailian MiniMax/MiniMax-M3 | adaptive toggle | `none` disables; enabled levels use adaptive thinking; sampling parameters are fixed |
| Bailian stepfun/step-3.7-flash | toggle and effort | `none` disables; minimal/low -> low; medium -> medium; high/xhigh/max -> high |
| TokenHub Hy3 | native effort | `none` disables; minimal/low -> low; medium -> medium; high/xhigh/max -> high |
| TokenHub DeepSeek V4 Flash | native effort | `none` disables; minimal/low -> low; medium/high -> high; xhigh/max -> max |
| TokenHub GLM 5.3 | always-on effort | enabled levels collapse to low/high/max |
| TokenHub Kimi K3 | always-on effort | all enabled levels map to max |
| DeepSeek R1 / QwQ Plus | fixed | provider ignores effort controls; explicit values are rejected |
| DeepSeek V3 / Qwen Max | unsupported | no controllable reasoning surface is exposed |

For Chat Completions adapters, Qwen-family toggle and budget controls use
`enable_thinking` and, where applicable, `thinking_budget`; native effort uses
`reasoning_effort`.  The adapter never sends `reasoning_effort` and
`thinking_budget` together.  OpenAI Responses uses `reasoning.effort`.
Anthropic and Gemini adapters translate to their native thinking objects and
must preserve their protocol-specific temperature restrictions.

Unsupported requests fail before network I/O with a normalized validation
error that identifies the selected model and unsupported choice.  Adapters must
never silently claim that reasoning is off for an always-on model.  Capability
metadata returned with model catalog rows lets the UI disable impossible
choices while the backend remains authoritative.

The capability table combines the provider's current model documentation with
the exact production endpoint's observed contract.  In particular, the current
Qwen 3.7 Max alias is not grouped with its older thinking-only preview.  The
Qwen 3.8 2.4T documentation describes hybrid thinking, but the enabled
production endpoint currently rejects `enable_thinking=false` with a provider
validation error restricting the field to `true`; the platform therefore keeps
`none` unavailable until a live regression proves that endpoint changed.  See
the official DashScope documentation for
[deep-thinking models](https://help.aliyun.com/zh/model-studio/deep-thinking),
[Qwen 3.7 Max](https://help.aliyun.com/zh/model-studio/qwen3-7-max),
[Qwen 3.8 2.4T A95B](https://help.aliyun.com/zh/model-studio/qwen3-8-2-4t-a95b),
[GLM](https://help.aliyun.com/zh/model-studio/glm-zhipu), and
[Kimi](https://help.aliyun.com/zh/model-studio/kimi-api).  Live probes remain
validation evidence; a local preflight rejection alone is never evidence that
the provider itself rejects a control, while an explicit provider response is.

## Context and accounting

Reasoning tokens are output-side consumption even when providers do not expose
their text.  The platform records provider-reported reasoning and output usage
where available and does not estimate billable usage from visible content.
Configured `max_output_tokens` remains the provider output ceiling.  A budget
adapter clamps its translated thinking budget below that ceiling and the
model-family cap; an impossible combination fails validation rather than
silently consuming the whole response allowance.

Prompt compaction continues to use the model's configured context window and
usage ratio.  Reasoning configuration must not reduce the protected current
turn or alter historical compaction boundaries.

## Validation contract

Implementation validation has three layers, all run in Docker:

1. Unit tests assert the canonical validator, inheritance precedence, model
   capability detection, and exact provider payloads without network calls.
2. API and persistence tests assert create/update/clear behavior, accepted-turn
   snapshots, recovery, failover, and every background-resource surface.
3. A credential-aware live regression securely streams the exact enabled
   production model inventory into the local backend validator over standard
   input.  No exported credential is written to disk, logged, or inserted into
   a shared development database.  Locally reconstructed clients exercise
   inheritance, one supported enabled effort, and `none` where the capability
   claims disable is supported.  Always-on/fixed models assert the documented
   local rejection for impossible choices.  The report identifies each model
   and case without printing keys, secrets, or reasoning content.

For comparative regressions, the same validator's all-efforts mode uses one
fixed prompt for every supported level and records first-event time,
first-visible-output time, total latency, exposed reasoning/output character
counts, and provider usage metadata.  Model-specific configured output limits
must cap the probe so a measurement cannot fail merely because a shared test
limit exceeds the provider's accepted `max_tokens` range.  These are sampled
observations, not product latency guarantees; capability claims are based on
whether controls are honored, not whether one stochastic sample is monotonic.

The live matrix is release evidence, not a normal hermetic test dependency.  A
release is blocked if any enabled production model is absent from the streamed
inventory or lacks a successful applicable live case.  Production is inspected
read-only for configuration; all client construction and test execution run in
the isolated local Docker environment.

Rollback keeps the additive database columns, but an older binary must not see
the newer reasoning arguments in persisted builtin tool schemas.  Before the
legacy application roles start, run the candidate image's idempotent
`python -m app.scripts.rollback_reasoning_controls apply` helper and verify
`status` reports zero reasoning fields.  A later candidate startup restores the
current schemas through the normal builtin seeder.


## Unified company model pool

`LLMModel` is the company-owned connection and capability record for both
conversation and media models. `api_protocol` selects the wire protocol
independently of `provider`; null preserves the registry behavior for existing
records, including the `openai-response` alias. Explicit protocol selection is
carried through runtime snapshots, caller retries, compaction, heartbeat and
connection tests. New OpenAI and Qwen records prefer Responses; Hunyuan and
Volcengine already use that registry default. Administrators retain explicit
Chat selection for models whose Responses endpoint cannot complete a tool turn.
Provider errors do not silently replay a partially executed turn through Chat.

`purposes` identifies conversation, media understanding, image generation,
audio generation, video generation or speech recognition. `input_modalities` identifies text,
image, audio and video inputs; migration preserves legacy `supports_vision`.
Conversation pickers and backend provisioning/runtime selection exclude models
without the conversation purpose. Generation-only models cannot become the
company conversation default.

The five media default model IDs reuse tenant `tool_config:media_ai` storage,
managed in the company model pool. Tool settings can override by purpose and
otherwise inherit those defaults. Media model tests select an existing Digital
Employee workspace and execute through the shared asynchronous media runtime;
results use the existing session viewer and task card, without another queue.

The builtin `list_models` exposes enabled models from the executing Agent's
tenant, with optional name, purpose, input-modality and serving-provider filters
and offset pagination. It returns IDs, capability labels and configured inference
defaults, never connection credentials or headers. Existing explicit tool
enablement and scene scope apply. It does not probe providers or promise capacity.
Both `read_media` and `generate_media` accept an optional enterprise `model_id`
and `parameters` object. Understanding parameters survive durable task admission
and are applied over model defaults through the shared Chat/Responses adapters;
`max_output_tokens` maps to the selected protocol, and explicit native reasoning
controls take precedence over the inherited normalized effort. Connection,
content and streaming lifecycle remain owned by their existing tool/runtime
fields. No provider-specific inference-parameter allowlist is introduced.

Legacy inline media connections and image-tool purposes migrate once. Internal
tenant migration receipts bind old references to model IDs, so administrator
changes govern every new admission and deletion returns `modelUnavailable`.
Already accepted jobs retain their encrypted model snapshot. Missing receipts
from a previously deployed conversion use conservative compatibility: reuse a
matching existing connection, but never recreate a removed or edited connection
or grant a removed purpose. Administrators resolve ambiguous old scene settings
by selecting an explicit enterprise model; ordinary model creation is unaffected.

Speech input resolves `speech_model_id` from the same company defaults and reads
the selected enterprise model's connection. Legacy speech-service records are
migrated once into model records; clearing a selection or disabling a model must
not reactivate the legacy configuration. The browser's PCM/stop event contract
is preserved: the DashScope adapter provides live partial transcripts, while the
standard audio-transcriptions adapter returns the final transcript after stop.
Speech input does not introduce a second model pool or task scheduler.

The model list shares adaptive Grid tracks across rows, including conditional
actions. Default selections for conversation and all media purposes live in the
default-model drawer; rows show a default badge and a labelled enable switch.
Model editing uses the shared settings drawer and form primitives;
imagination and reasoning controls share one responsive two-column field grid. Imagination
uses `DivergenceSlider`; reasoning uses `ReasoningEffortSelect` with Auto retaining
its inheritance semantics. Editing a different provider/model/endpoint must not
apply the old model's capability choices. All labels use shared i18n resources.

The provider registry includes Tencent Hunyuan and Volcengine Ark. Their current
default endpoints support Responses; administrators can select Chat Completions
and a different endpoint for compatible or legacy models. Provider registration
does not imply that every model supports every media or reasoning capability.

Serving-platform labels are a read-only projection (`service_platform`) of the
configured endpoint and provider. Known TokenHub, Bailian and Ark hosts identify
the serving platform independently of model branding; unknown services retain
the configured provider. This does not rewrite legacy provider values, protocols,
credentials or default references. The model pool and all model pickers share
localized model-name/platform presentation, sorting by model name then platform.
TokenHub has its own registry entry with a Responses default; individual model
capabilities still determine the actual transport and accepted parameters.

TokenHub reasoning mappings follow its [Responses contract](https://cloud.tencent.com/document/product/1823/135873)
and model-specific [GLM](https://cloud.tencent.com/document/product/1823/132061),
[Kimi](https://cloud.tencent.com/document/product/1823/132232) and
[DeepSeek](https://cloud.tencent.com/document/product/1823/132248) contracts.
Bailian direct-provider IDs use their own [MiniMax](https://help.aliyun.com/zh/model-studio/minimax-api-by-minimax)
and [Step](https://help.aliyun.com/zh/model-studio/stepfun) parameter contracts.
In particular, do not copy Bailian Kimi effort values into TokenHub Kimi K3.

Enterprise models expose optional `extra_headers` through the existing model
form and connection test. Values are encrypted at rest; only model administrators
receive decrypted configuration, and ordinary model selectors receive an empty
map. Omitted updates preserve the configuration, `null` inherits endpoint
defaults, and `{}` explicitly clears optional headers. Clones and accepted media
jobs retain their encrypted configuration snapshot. Custom headers never belong
in public task metadata or third-party artifact downloads; cross-origin LLM
redirects strip configured headers.

The Bailian endpoint preset is `X-DashScope-Wait-Timeout: 120`, following its
[official request-header contract](https://help.aliyun.com/zh/model-studio/rate-limiting-best-practices).
Administrators can change or remove it like any other header. All LLM protocols,
compaction, media adapters and speech transports consume the shared configuration.
This is provider request metadata only: it adds no platform queuing, retry,
overall timeout or model-switch policy, and preserves existing transport timers.
