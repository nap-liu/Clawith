PROXY_SERVER="$(echo -n "$PROXY_SERVER" | xargs)"
if [ -n "${PROXY_SERVER}" ]; then
  PROXY_SERVER=${PROXY_SERVER#\"}
  PROXY_SERVER=${PROXY_SERVER%\"}
  PROXY_SERVER=${PROXY_SERVER#http://}
  PROXY_SERVER=${PROXY_SERVER#https://}

  GOST_CONFIG="/opt/gem/gost.yaml"
  GOST_HOSTS_FILE="/opt/gem/gost-hosts.txt"
  GOST_BYPASS_FILE="/opt/gem/gost-bypass.txt"
  PROXY_MAP_JSON="/var/lib/aio-sandbox/proxy-map.json"
  NGINX_PROXY_MAP_CONF="/opt/gem/nginx-proxy-map.conf"
  PROXY_PORT="${TINYPROXY_PORT:-8118}"
  NGINX_PROXY_MAP_PORT=80

  USER_BYPASS_JSON="/var/lib/aio-sandbox/proxy-map-user-bypasses.json"

  # Initialize files
  > "${GOST_HOSTS_FILE}"
  > "${GOST_BYPASS_FILE}"
  > "${NGINX_PROXY_MAP_CONF}"
  echo "[]" > "${PROXY_MAP_JSON}"
  echo "[]" > "${USER_BYPASS_JSON}"

  # --- Generate self-signed TLS cert for proxy-map HTTPS termination ---
  PROXY_MAP_TLS_KEY="/opt/gem/proxy-map-tls.key"
  PROXY_MAP_TLS_CRT="/opt/gem/proxy-map-tls.crt"

  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${PROXY_MAP_TLS_KEY}" \
    -out "${PROXY_MAP_TLS_CRT}" \
    -days 3650 -subj "/CN=AIO Sandbox Proxy Map" \
    2>/dev/null

  # Extract SPKI hash — tells Chromium to trust this specific key for any hostname
  PROXY_MAP_SPKI=$(openssl x509 -in "${PROXY_MAP_TLS_CRT}" -pubkey -noout \
    | openssl pkey -pubin -outform DER \
    | openssl dgst -sha256 -binary \
    | base64)

  log "Generated proxy-map TLS cert (SPKI: ${PROXY_MAP_SPKI})"

  # --- PROXY_AUTH_CMD: execute a shell command to obtain proxy credentials ---
  # The command stdout should be "username:password".
  # The result is prepended to PROXY_SERVER as user:pass@host:port.
  PROXY_AUTH_CMD="${PROXY_AUTH_CMD:-}"
  if [ -n "${PROXY_AUTH_CMD}" ] && [ "${PROXY_SERVER}" != "true" ]; then
    PROXY_AUTH_STDERR=$(mktemp)
    PROXY_AUTH_RESULT=$(bash -c "${PROXY_AUTH_CMD}" 2>"${PROXY_AUTH_STDERR}") || {
      log "WARNING: PROXY_AUTH_CMD failed: $(cat "${PROXY_AUTH_STDERR}")"
    }
    rm -f "${PROXY_AUTH_STDERR}"
    if [ -n "${PROXY_AUTH_RESULT}" ]; then
      # Strip any existing auth from PROXY_SERVER before injecting
      PROXY_SERVER_CLEAN="${PROXY_SERVER}"
      if [[ "${PROXY_SERVER_CLEAN}" == *"@"* ]]; then
        PROXY_SERVER_CLEAN="${PROXY_SERVER_CLEAN##*@}"
      fi
      PROXY_SERVER="${PROXY_AUTH_RESULT}@${PROXY_SERVER_CLEAN}"
      log "PROXY_AUTH_CMD: injected auth into PROXY_SERVER (addr=${PROXY_SERVER_CLEAN})"
    else
      log "WARNING: PROXY_AUTH_CMD returned empty output, skipping auth injection"
    fi
  fi

  # --- Parse PROXY_SERVER auth credentials ---
  # PROXY_SERVER may be in the format user:pass@host:port (e.g. from rd-proxy/ZTI).
  # GOST requires addr to be host:port only; auth goes in connector.auth.
  PROXY_ADDR="${PROXY_SERVER}"
  PROXY_AUTH_USER=""
  PROXY_AUTH_PASS=""
  if [[ "${PROXY_SERVER}" == *"@"* ]] && [ "${PROXY_SERVER}" != "true" ]; then
    PROXY_AUTH_PART="${PROXY_SERVER%@*}"
    PROXY_ADDR="${PROXY_SERVER##*@}"
    # Only parse auth if the part before @ contains a colon (user:pass)
    if [[ "${PROXY_AUTH_PART}" == *":"* ]]; then
      PROXY_AUTH_USER="${PROXY_AUTH_PART%%:*}"
      PROXY_AUTH_PASS="${PROXY_AUTH_PART#*:}"
    fi
    if [ -n "${PROXY_AUTH_USER}" ] && [ -n "${PROXY_AUTH_PASS}" ]; then
      log "Parsed PROXY_SERVER: addr=${PROXY_ADDR}, auth_user=${PROXY_AUTH_USER:0:10}..."
    else
      log "WARNING: PROXY_SERVER contains '@' but auth credentials are incomplete, ignoring auth"
      PROXY_AUTH_USER=""
      PROXY_AUTH_PASS=""
    fi
  fi

  # --- Generate GOST config ---
  cat > "${GOST_CONFIG}" <<GOST_YAML_EOF
api:
  addr: "127.0.0.1:18080"

services:
  - name: browser-proxy
    addr: "127.0.0.1:${PROXY_PORT}"
    handler:
      type: http
$(if [ "${PROXY_SERVER}" != "true" ]; then echo "      chain: upstream"; fi)
    listener:
      type: tcp
    hosts: proxy-map

$(if [ "${PROXY_SERVER}" != "true" ]; then cat <<CHAIN_INNER_EOF
chains:
  - name: upstream
    hops:
      - name: hop-0
        bypass: proxy-bypass
        nodes:
          - name: upstream-proxy
            addr: "${PROXY_ADDR}"
            connector:
              type: http
$(if [ -n "${PROXY_AUTH_USER}" ]; then cat <<AUTH_EOF
              auth:
                username: "${PROXY_AUTH_USER}"
                password: "${PROXY_AUTH_PASS}"
AUTH_EOF
fi)
            dialer:
              type: tcp
CHAIN_INNER_EOF
fi)

hosts:
  - name: proxy-map
    reload: 3s
    file:
      path: ${GOST_HOSTS_FILE}

bypasses:
  - name: proxy-bypass
    reload: 3s
    file:
      path: ${GOST_BYPASS_FILE}
GOST_YAML_EOF

  log "GOST config generated at ${GOST_CONFIG}"

  # --- Parse PROXY_MAP (domain grouping: same domain -> one nginx server block) ---
  #
  # Format: source>target[,source>target,...]
  #   source: [protocol://]host[:port][/path]  (supports wildcard * in host)
  #   target: [host:]port[/path]  (host defaults to 127.0.0.1)
  #
  # Same-domain entries are grouped into one nginx server block with multiple locations.
  #
  PROXY_MAP_VAL="$(echo -n "${PROXY_MAP:-}" | xargs)"
  if [ -n "${PROXY_MAP_VAL}" ]; then
    # We need associative arrays for domain grouping
    declare -A DOMAIN_LOCATIONS
    declare -A DOMAIN_SEEN
    MAPPINGS_JSON="["
    FIRST=true

    IFS=',' read -ra MAP_ENTRIES <<< "$PROXY_MAP_VAL"
    for entry in "${MAP_ENTRIES[@]}"; do
      entry=$(echo "$entry" | xargs)
      [ -z "$entry" ] && continue
      source_part="${entry%%>*}"
      target_part="${entry##*>}"

      if [ -z "$source_part" ] || [ -z "$target_part" ]; then
        log "WARNING: Invalid PROXY_MAP entry '${entry}', skipping"
        continue
      fi

      source_host="${source_part}"
      source_host="${source_host#*://}"
      source_host="${source_host%%/*}"
      if echo "$source_host" | grep -qE ':[0-9]+$' && ! echo "$source_host" | grep -q '^\*'; then
        source_port_part="${source_host##*:}"
        log "WARNING: Source port :${source_port_part} in '${source_part}' is ignored — all ports for this domain will be mapped"
      fi
      source_host="${source_host%%:*}"

      source_path="/"
      source_raw_no_proto="${source_part#*://}"
      if echo "$source_raw_no_proto" | grep -q '/'; then
        source_path="/${source_raw_no_proto#*/}"
        source_path="${source_path%\*}"
        if [ "$source_path" != "/" ] && [ "${source_path: -1}" != "/" ]; then
          source_path="${source_path}/"
        fi
      fi

      if [ "${target_part:0:1}" = ":" ]; then
        target_part="127.0.0.1${target_part}"
      fi
      target_host_port="${target_part%%/*}"
      target_path=""
      if echo "$target_part" | grep -q '/'; then
        target_path="/${target_part#*/}"
        target_path="${target_path%\*}"
      fi

      if [ -z "${DOMAIN_SEEN[$source_host]+_}" ]; then
        DOMAIN_SEEN[$source_host]=1
        DOMAIN_LOCATIONS[$source_host]=""
      fi

      DOMAIN_LOCATIONS[$source_host]+="
        location ${source_path} {
            proxy_pass http://${target_host_port}${target_path};
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_http_version 1.1;
            proxy_read_timeout 600s;
            proxy_send_timeout 600s;
        }"

      if [ "$FIRST" = true ]; then FIRST=false; else MAPPINGS_JSON+=","; fi
      MAPPINGS_JSON+="{\"source\":\"${source_part}\",\"target\":\"${target_part}\",\"source_host\":\"${source_host}\",\"source_path\":\"${source_path}\",\"internal_port\":${NGINX_PROXY_MAP_PORT}}"

      log "Proxy map: ${source_part} -> ${target_part} (domain group ${source_host} via nginx :${NGINX_PROXY_MAP_PORT})"
    done

    MAPPINGS_JSON+="]"
    echo "${MAPPINGS_JSON}" > "${PROXY_MAP_JSON}"

    for domain in "${!DOMAIN_SEEN[@]}"; do
      gost_host="${domain}"
      if [ "${gost_host:0:2}" = "*." ]; then
        gost_host="${gost_host:1}"
      fi
      echo "127.0.0.1 ${gost_host}" >> "${GOST_HOSTS_FILE}"

      echo "${domain}" >> "${GOST_BYPASS_FILE}"

      cat >> "${NGINX_PROXY_MAP_CONF}" <<NGINX_BLOCK_EOF
server {
    listen 127.0.0.1:${NGINX_PROXY_MAP_PORT};
    listen 127.0.0.1:443 ssl;
    ssl_certificate ${PROXY_MAP_TLS_CRT};
    ssl_certificate_key ${PROXY_MAP_TLS_KEY};
    server_name ${domain};
    client_max_body_size 0;
${DOMAIN_LOCATIONS[$domain]}
}

NGINX_BLOCK_EOF
    done

    echo "127.0.0.1" >> "${GOST_BYPASS_FILE}"
  fi

  PROXY_EXCLUDE_VAL="$(echo -n "${PROXY_EXCLUDE:-}" | xargs)"
  if [ -n "${PROXY_EXCLUDE_VAL}" ]; then
    BYPASS_JSON="["
    FIRST_BYPASS=true
    IFS=',' read -ra EXCLUDES <<< "$PROXY_EXCLUDE_VAL"
    for pattern in "${EXCLUDES[@]}"; do
      pattern=$(echo "$pattern" | xargs)
      echo "${pattern}" >> "${GOST_BYPASS_FILE}"
      if [ "$FIRST_BYPASS" = true ]; then FIRST_BYPASS=false; else BYPASS_JSON+=","; fi
      BYPASS_JSON+="\"${pattern}\""
    done
    BYPASS_JSON+="]"
    echo "${BYPASS_JSON}" > "${USER_BYPASS_JSON}"
    log "GOST bypass (exclude): ${PROXY_EXCLUDE_VAL}"
  fi

  PROXY_INCLUDE_VAL="$(echo -n "${PROXY_INCLUDE:-}" | xargs)"
  if [ -n "${PROXY_INCLUDE_VAL}" ]; then
    PROXY_MAP_BYPASSES=""
    if [ -n "${PROXY_MAP_VAL}" ]; then
      for domain in "${!DOMAIN_SEEN[@]}"; do
        PROXY_MAP_BYPASSES+="${domain}"$'\n'
      done
      PROXY_MAP_BYPASSES+="127.0.0.1"$'\n'
    fi
    echo -n "${PROXY_MAP_BYPASSES}" > "${GOST_BYPASS_FILE}"

    BYPASS_JSON="["
    FIRST_BYPASS=true
    IFS=',' read -ra INCLUDES <<< "$PROXY_INCLUDE_VAL"
    for pattern in "${INCLUDES[@]}"; do
      pattern=$(echo "$pattern" | xargs)
      echo "${pattern}" >> "${GOST_BYPASS_FILE}"
      if [ "$FIRST_BYPASS" = true ]; then FIRST_BYPASS=false; else BYPASS_JSON+=","; fi
      BYPASS_JSON+="\"${pattern}\""
    done
    BYPASS_JSON+="]"
    echo "${BYPASS_JSON}" > "${USER_BYPASS_JSON}"
    sed -i 's/name: proxy-bypass/name: proxy-bypass\n    whitelist: true/' "${GOST_CONFIG}"
    log "GOST bypass (include/whitelist): ${PROXY_INCLUDE_VAL}"
  fi

  TRIMMED_BYPASS="$(echo -n "${PROXY_BYPASS_LIST:-}" | xargs)"
  if [ -n "${TRIMMED_BYPASS}" ]; then
    PAC_FILE="/opt/gem/proxy.pac"
    {
      echo 'function FindProxyForURL(url, host) {'
      IFS=',' read -ra BYPASS_DOMAINS <<< "$TRIMMED_BYPASS"
      for domain in "${BYPASS_DOMAINS[@]}"; do
        domain=$(echo "$domain" | xargs)
        clean_domain="${domain#\*.}"
        echo "  if (dnsDomainIs(host, \"${clean_domain}\") || host === \"${clean_domain}\") return \"DIRECT\";"
      done
      echo "  return \"PROXY 127.0.0.1:${PROXY_PORT}\";"
      echo '}'
    } > "$PAC_FILE"
    export BROWSER_EXTRA_ARGS="${BROWSER_EXTRA_ARGS} --proxy-pac-url=file://${PAC_FILE}"
    log "Generated PAC file at ${PAC_FILE} with bypass: ${TRIMMED_BYPASS}"
  else
    export BROWSER_EXTRA_ARGS="${BROWSER_EXTRA_ARGS} --proxy-server=http://127.0.0.1:${PROXY_PORT}"
  fi

  export BROWSER_EXTRA_ARGS="${BROWSER_EXTRA_ARGS} --ignore-certificate-errors-spki-list=${PROXY_MAP_SPKI}"

  chown $USER:$USER "${GOST_HOSTS_FILE}" "${GOST_BYPASS_FILE}" "${PROXY_MAP_JSON}" "${NGINX_PROXY_MAP_CONF}" "${USER_BYPASS_JSON}" 2>/dev/null || true
  chmod 644 "${PROXY_MAP_TLS_CRT}" 2>/dev/null || true
  chmod 600 "${PROXY_MAP_TLS_KEY}" 2>/dev/null || true
else
  rm -f /opt/gem/supervisord/supervisord.gost.conf
  touch /opt/gem/nginx-proxy-map.conf
fi
