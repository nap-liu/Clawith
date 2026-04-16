# TODO: egress allowlist — kernel-level enforcement (phase 2)

**Status:** phase 1 (pass-through only) shipped. Phase 2 is open.

## What exists today (phase 1)

`SandboxConfig.egress_allowlist` is a `list[str]` of hostnames. When non-empty
and `network=True`, the executor injects the comma-separated list into the
sandbox environment as `CLAWITH_EGRESS_ALLOWLIST`. Schema-validated to
`^[a-z0-9.-]+$` so shell / rule-file injection is not possible via that
variable.

Both sandbox backends (`DockerSandboxBackend`, `BubblewrapBackend`) currently
**pass the variable through** to the sandboxed binary and nothing else. Their
class docstrings document this limitation explicitly.

## What this does NOT do

**It is not kernel-level or network-level enforcement.** A cooperative CLI
(something we ship, wrapped around `httpx`/`requests`/`fetch`) can read the
env var and restrict itself. A hostile or LLM-driven binary can ignore the
variable and `connect()` anywhere the host network permits. The LLM prompt-
injection vector that motivated this field (tool exfiltrates the environment
to an attacker host) is therefore **not closed** yet.

Operator docs (frontend UI hint, class docstrings, this file) all call this
out so nobody walks away with a false sense of security.

## Phase 2: actual enforcement

Two parallel work streams. Pick one per backend; the frontend contract does
not change (same `egress_allowlist` field).

### Docker backend — tinyproxy sidecar

1. Build & publish a `clawith-cli-egress-proxy` image from
   `backend/cli_sandbox/egress_proxy/`:
   - Alpine + `tinyproxy`
   - entrypoint reads `ALLOW_HOSTS` env, renders `/etc/tinyproxy/tinyproxy.conf`
     with `Filter` / `FilterDefaultDeny Yes` rules.
2. Per-execute sidecar: when `egress_allowlist` is non-empty, the
   `DockerSandboxBackend` starts a proxy container on a dedicated
   user-defined bridge, puts the sandbox on the same bridge, and exports
   `HTTPS_PROXY=http://<sidecar>:8888` / `HTTP_PROXY=…` / `NO_PROXY=localhost`
   into the sandbox env. Also drops the sandbox's direct egress via
   `--network` isolation (so unproxied TCP still fails).
3. Tear down the sidecar in `finally` — even on timeout / exception paths.
4. Alternative to investigate: `--dns` pointing at a scoped `dnsmasq`
   permits only allowed hosts to resolve. Cheaper than a proxy, but only
   protects against hostname-based egress; attackers who know IPs still win.

**Attack surface that remains after phase 2:** a proxy-based scheme only
enforces HTTP(S). Raw TCP to an IP literal bypasses the proxy. Close it by
pairing the proxy with `iptables OUTPUT DROP` on the sandbox's bridge —
requires `NET_ADMIN` on the backend container or docker daemon policy.

### Bubblewrap backend — network namespace + nftables

`bwrap` itself has no egress primitive. Plan:

1. Create a dedicated network namespace per run (`ip netns add …`), set up
   a veth pair, NAT through the host.
2. Populate the namespace's `nft` ruleset:
   ```
   table inet egress {
     set allowed { type ipv4_addr; flags dynamic, timeout; }
     chain output {
       type filter hook output priority 0; policy drop;
       ip daddr @allowed accept
       # plus localhost / link-local / dns resolver IP
     }
   }
   ```
3. A small resolver sidecar watches DNS responses for allowlisted hosts
   and populates `@allowed` with the returned IPs (short timeout so
   revocations take effect).
4. `bwrap` joins the prepared netns with `--share-net` — which now
   inherits the restricted namespace instead of the host's.

Needs `NET_ADMIN` on the backend process and a Linux kernel with nftables
(all supported targets). macOS dev environments keep the pass-through
behaviour.

## Integration point

The frontend and schema contract (`egress_allowlist: list[str]`) is stable.
Phase 2 consumes the same value; no schema, API, or frontend changes are
required when enforcement lands. Any follow-up PR that adds the sidecar or
nftables path should:

- Remove the "pass-through only" warning from the class docstrings and the
  frontend hint.
- Add an integration test that spins up the backend, runs a binary with
  `network=True` + allowlist `["api.yeyecha.com"]`, and verifies a
  connection to any other host (`curl https://example.com`) fails.

## Who owns this

Infra / platform team — this is a cross-cutting network-policy change that
depends on deploy topology (docker-compose vs k8s), not a cli-tools feature.
