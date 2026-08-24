#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
approved_registries='docker\.m\.daocloud\.io|enterprise-public-cn-beijing\.cr\.volces\.com|\.Values\.'
configured_registry=${CLAWITH_IMAGE_MIRROR:-docker.m.daocloud.io}

case "$configured_registry" in
    docker.m.daocloud.io | \
        enterprise-public-cn-beijing.cr.volces.com | \
        registry.cn-*.aliyuncs.com* | \
        ccr.ccs.tencentyun.com* | \
        swr.cn-*.myhuaweicloud.com*) ;;
    *)
        echo "Docker image policy violation: CLAWITH_IMAGE_MIRROR must be a domestic registry." >&2
        exit 1
        ;;
esac

invalid_images=$(
    grep -RHE '^[[:space:]]*image:[[:space:]]*[^[:space:]]' \
        "$repository_root/docker-compose.yml" \
        "$repository_root/docker-compose.aio-sandbox.yml" \
        "$repository_root/deploy" \
        "$repository_root/helm/clawith" \
        "$repository_root/.github" \
        | grep -Ev "$approved_registries" \
        || true
)

invalid_from=$(
    find "$repository_root" \
        -path "$repository_root/backend/agent_data" -prune -o \
        -path "$repository_root/.git" -prune -o \
        -path "$repository_root/.worktrees" -prune -o \
        -name 'Dockerfile*' -type f -exec grep -HE '^FROM[[:space:]]+' {} + \
        | grep -Ev 'CLAWITH_IMAGE_MIRROR|enterprise-public-cn-beijing\.cr\.volces\.com|docker\.m\.daocloud\.io' \
        || true
)

if [ -n "$invalid_images" ] || [ -n "$invalid_from" ]; then
    echo "Docker image policy violation: every image must use an approved domestic registry." >&2
    [ -z "$invalid_images" ] || echo "$invalid_images" >&2
    [ -z "$invalid_from" ] || echo "$invalid_from" >&2
    exit 1
fi

http_proxy=$(docker info --format '{{.HTTPProxy}}' 2>/dev/null || true)
https_proxy=$(docker info --format '{{.HTTPSProxy}}' 2>/dev/null || true)
if [ -n "$http_proxy" ] || [ -n "$https_proxy" ]; then
    echo "Docker image policy violation: disable the Docker daemon HTTP/HTTPS proxy before pulling images." >&2
    exit 1
fi

echo "Docker image policy passed: domestic registries only and daemon proxy disabled."
