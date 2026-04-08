# ─── Backend Dockerfile (Optimized) ─────────────────────
# Multi-stage: deps → production
FROM python:3.12-slim AS deps

WORKDIR /app
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
ARG CLAWITH_PIP_INDEX_URL
ARG CLAWITH_PIP_TRUSTED_HOST
RUN if [ -n "$CLAWITH_PIP_INDEX_URL" ] && [ -n "$CLAWITH_PIP_TRUSTED_HOST" ]; then \
        pip install --no-cache-dir --index-url "$CLAWITH_PIP_INDEX_URL" --trusted-host "$CLAWITH_PIP_TRUSTED_HOST" .; \
    elif [ -n "$CLAWITH_PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --index-url "$CLAWITH_PIP_INDEX_URL" .; \
    else \
        pip install --no-cache-dir .; \
    fi

# ─── Production ─────────────────────────────────────────
FROM python:3.12-slim AS production

WORKDIR /app
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl shadowsocks-libev gosu && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=deps /usr/local/bin/ /usr/local/bin/

# Copy application code
COPY . .

# Copy svc CLI binary
COPY svc /usr/local/bin/svc
RUN chmod +x /usr/local/bin/svc

RUN useradd --create-home clawith && \
    mkdir -p /data/agents && \
    chmod +x /app/entrypoint.sh && \
    chown -R clawith:clawith /app /data

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
