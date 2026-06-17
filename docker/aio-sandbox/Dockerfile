FROM enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.9.3
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
