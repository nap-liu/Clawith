FROM enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.9.3
# Guard the lifecycle patch against silent upstream drift. If the base tag is
# ever republished with a different OpenHands shell implementation, fail the
# build and rebase the patch deliberately.
RUN echo "28dec0a9b8b53cd603ffc8468eb8d45b477d62aa6d23505cc3a026b219a6b9f4  /opt/python3.12/lib/python3.12/site-packages/vendors/openhands/runtime/utils/bash.py" \
    | sha256sum -c -
RUN echo "2319d2f771361250f4c101b920ceb82e7929507459b779579f7cbc1c3a1c4dae  /opt/python3.12/lib/python3.12/site-packages/app/api/v1/shell.py" \
    | sha256sum -c -
# Patch 1+2: stdio unpack fix + config hot-reload (cached_property -> property).
COPY mcp_client.py /opt/python3.12/lib/python3.12/site-packages/app/services/mcp_client.py
# Patch 3: make the generated mcp-hub.json writable by the `gem` API user so
# Clawith can register stdio MCP servers at runtime via the sandbox API
# (the only change vs upstream run.sh is one `chmod 666` after it generates the file).
COPY run.sh /opt/gem/run.sh
RUN chmod +x /opt/gem/run.sh
# Patch 4: stop masking real tool errors. The tool-call REST endpoint caught any
# exception and returned a generic "Failed to execute tool" 500 while logging the
# real error — so the upstream API message (e.g. "Yunxiao API error: invalid
# workitemTypeId") never reached the caller, and the LLM kept blindly retrying.
# The only change vs upstream mcp.py is appending `: {e}` to the 500 detail.
COPY mcp.py /opt/python3.12/lib/python3.12/site-packages/app/api/v1/mcp.py
# Patch 5: make the existing /v1/shell timeout actually terminate the
# foreground process group while keeping the stateful tmux shell and prior
# background jobs. Explicit session close also reaps all processes still
# attached to the pane's Linux process session, including resistant children.
COPY bash.py /opt/python3.12/lib/python3.12/site-packages/vendors/openhands/runtime/utils/bash.py
COPY shell.py /opt/python3.12/lib/python3.12/site-packages/app/api/v1/shell.py
COPY tests/ /opt/clawith-tests/
