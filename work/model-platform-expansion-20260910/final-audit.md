# Final independent change audit — 2026-09-10

## Verdict

No open blocking finding remains in the reviewed scope, including the final user-requested change making capability metadata advisory. One P2 correctness issue was found in generation parameter inheritance, reported to the implementing agent, and closed after reviewing its final fix. No additional framework, queue, provider table, or authorization layer is required by this audit.

This reviewer did not implement the backend product changes. The review covered the current working-tree diff and new modules for generation adapters, configurable request headers, IM-group subagent execution identity, and media input/model selection. It does not substitute for the final 3008 integration run or authorize production deployment.

## Closed finding: current native options were overwritten by prior-turn defaults

Before the fix, a previous generation with top-level `duration: 5` followed by `parameters: {duration: 3}` inherited the old top-level duration, which the adapter then used to override the current native value. Image size and audio voice had the same precedence problem.

`backend/app/services/media_ai_context.py` now captures the current turn's explicitly supplied parameter map before inheriting native settings. `_native_override` prevents inheritance of overlapping normalized controls, including `seconds`, `aspect_ratio`, `size`, nested TokenHub `settings`, `voice_setting.voice_id`, and `timbre_weights`. A current explicit top-level value continues to take precedence over a current native value. The change is narrow and uses the existing preparation path.

Reviewed `backend/tests/test_media_generation_precedence.py`: six parameterized two-turn worker cases assert the final outgoing provider payload for Bailian, standard OpenAI-compatible video, TokenHub video, TokenHub speech, image size, and explicit top-level precedence. These are observable worker/provider-boundary assertions, not source-shape tests. The implementing agent reports 16 focused Docker checks passed; this reviewer did not rerun the matrix. The older private `context-final-test.log` contains earlier failures and is not used as evidence for this final fix.

## Other reviewed boundaries

- **Execution authority:** IM-group origin is created from the server's running parent turn and actual message sender, not tool arguments. The child retains the execution user. The fallback checks the agent/session/anchor relationship, tenant, group IM channel, active user/identity, human sender, and admitted turn metadata. Execution revalidates the origin. Project execution retains its existing project authority path. No cross-tenant or tool-argument forgery path was identified in this diff.
- **Model selection (final user requirement):** `input_modalities` only guides implicit default selection. An explicitly chosen, authorized model is retained and receives the actual media regardless of stale capability labels. If no metadata-compatible candidate exists, the preferred connection is still attempted; configured fallback connections are also not blocked by modality labels. Provider-level capability hard gates and the added `unsupportedInput` error were removed. Tenant ownership, enabled state, and purpose checks remain authoritative in `resolve_media_model`; automatic candidate queries still enforce those same boundaries. When an implicit better match is found after loading an opaque URL, its connection is encrypted and checkpointed before provider invocation. This replaces the earlier audit criterion that explicit metadata mismatches should be rejected.
- **Headers:** optional headers are encrypted in model storage and accepted-job snapshots. Ordinary model-list callers receive no decrypted header values. Existing update semantics distinguish omitted, null, and empty maps. Configured custom headers are stripped on cross-origin LLM redirects; third-party artifact downloads use the separate media downloader.
- **Generation and recovery:** provider differences remain in small adapters behind the existing media facade. Accepted provider task IDs use the existing durable job and read-only polling/recovery path. Reference lists and supported role ordering are retained. The single-artifact contract rejects known multi-artifact modes before paid submission rather than silently discarding requested results.
- **Context:** generation native settings inherit only for the same model and output type. Media understanding continues to use the shared history and compactor. The precedence fix preserves the current prompt and current explicit controls.

## Evidence and limits

Existing provider observations are recorded in `bailian-vision-live-results.json`, `bailian-omni-audio-live.jsonl`, `bailian-kimi-video-live.jsonl`, and the generation ledgers in this directory. Successful calls do not prove maximum context/video duration, perfect recognition quality, or provider availability under all loads. Provider failures and unverified capabilities remain explicit in those records.

This final pass was read-only for product code and used no production mutation or additional paid provider calls. Reviewed `group-origin-results.jsonl`: both real 3008 group-member jobs completed, using `qwen3.5-plus` for video and `qwen-omni-turbo` for audio; both retain the actual member identity without global private-agent access, and no external IM notification was sent. These observations precede the final metadata-advisory simplification and are evidence for the group identity/audio/video path, not a new post-change full matrix.

The updated `test_media_model_inputs.py` exercises explicit selection despite stale labels, default matching for a whole batch, retention of the preferred model when no better label match exists, actual provider acceptance/rejection, and configured fallback preservation. The reviewer checked these assertions and their production paths without rerunning the full suite. No additional restriction or new permission mechanism is recommended.

The two files involved in the P2 fix are 127 and 61 physical lines respectively, below the repository's 800-line gate. This audit record does not claim a new full-repository test run.

## Main executor's final verification supplement

After the independent review, the main executor ran the final affected Docker
combination: `final-media-regression.log` records 143 passed and four dependency
deprecation warnings. This scope includes group and same-user IM P2P admission,
foreign-P2P rejection, model-label advisory behavior, and generation precedence.
The final helper accepts a group origin or an IM P2P Session belonging to the
actual actor, while retaining the human anchor and tenant/active-state checks.

`stale-capability-live.jsonl` additionally records a real post-simplification
qwen3.5-plus video call with text/image-only labels; it returned the visually
verified black shirt color. No full model matrix was repeated. The line gate
checked 69 delivered source files with no violation; maximum length was 775.
