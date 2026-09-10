# Media tools and session viewer acceptance

Implemented in the current checkout and installed in the local port-3008 environment. This iteration has not been released to production.

- Added `list_models`, with name, purpose, input type and provider filters plus pagination. It returns enabled models belonging to the executing agent's company, including IDs, capabilities and defaults. It does not perform provider health probes.
- Both media tools accept an optional model ID and parameters. Understanding parameters now survive asynchronous admission and reach the existing Chat/Responses provider adapters. Generation retains its existing parameter support.
- Historical media requests render submitted files through shared attachment components. Exact message references participate in existing playback authorization. External URLs retain their signatures and bypass workspace download URLs.
- Image previews appear above the session drawer; Escape closes the preview before the drawer. Physical drawer dimensions were not changed.
- Tool and parameter descriptions describe usage concisely, without internal scheduling, transport or model-selection policy narration. Local seeded tool definitions were synchronized.

Validation:

- Docker backend regression: 99 passed. After final contract edits, focused catalog/parameter checks: 5 passed. Three existing dependency warnings.
- Docker frontend standard build, including its prebuild checks: passed. An added attachment test initially omitted the canonical `display_content` field; corrected the fixture and reran the build successfully.
- Actual local Xiaozhi tool dispatch returned filtered audio/video understanding models with pagination. Database definitions include `list_models` and both media parameter inputs.
- Docker browser on port 3008: historical attachments render; image preview stacking and Escape behavior pass; audio plays; H.264 video plays in WebKit. Test Chromium lacks H.264 support, so video playback was verified in WebKit.
- Final diff whitespace check passed. All 21 changed/new handwritten source files are at most 800 lines; maximum 723.

No new paid provider calls or production mutations were made for this iteration. Existing provider regression evidence is not claimed as a fresh live-model test.

Independent review follow-up: malformed `data:` input accepted for deferred decoding could raise during history attachment projection. Projection now skips that malformed item while retaining the message and other attachments. The existing Docker catalog/history tests include the admitted malformed input and pass (2 tests, 3 existing warnings). This follow-up is validated from the exact checkout in Docker; no additional restart of the shared port-3008 stack was performed.
