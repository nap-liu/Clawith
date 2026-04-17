# clawith-cli-sandbox runbook

Publishes the sandbox image used by the CLI tools subsystem to execute
user-uploaded binaries.

## Tag scheme

- `<registry>/clawith-cli-sandbox:debian-bookworm-slim-YYYYMMDD` — immutable, keep forever
- `<registry>/clawith-cli-sandbox:stable` — moving alias the backend resolves by default

Tools without `config.sandbox.image` set follow `:stable`. Tools that pin
a specific dated tag stay on that tag across platform upgrades.

## Publishing a new version

```
cd backend/cli_sandbox
export REGISTRY=<your-registry-host>

# 1. Build + push the dated tag
make push

# 2. Validate in staging (see §2 Validation)

# 3. Promote to stable only after validation
make promote-stable
```

## Validation before promoting to :stable

1. Configure a staging environment to use the new dated tag (pin it on the
   CLI tool via UI or API PATCH).
2. Run `POST /api/tools/{id}/test-run` for every CLI tool against the new
   image.
3. Watch the backend logs for 10 minutes for `SANDBOX_FAILED` or
   `BINARY_FAILED` error classes.
4. If clean: `make promote-stable`. If not: skip promotion; investigate.

## Rollback

To revert `:stable` to a previous dated tag:

```
docker pull <registry>/clawith-cli-sandbox:<previous-date-tag>
docker tag <registry>/clawith-cli-sandbox:<previous-date-tag> <registry>/clawith-cli-sandbox:stable
docker push <registry>/clawith-cli-sandbox:stable
```

Tools that pinned a specific tag are unaffected by rollback.
