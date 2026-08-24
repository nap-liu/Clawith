# Release rules

- The semantic version prefix follows the current upstream release. Private changes are identified by the exact commit SHA suffix; do not invent a higher private semantic version.
- Build and publish backend and frontend from the same exact release SHA, even when only one side changed.
- Build production-bound images for `linux/amd64`. Apple Silicon local images are not production artifacts.
- Rebuild AIO sandbox only when its source/base image changes or the user explicitly requests it; otherwise preserve its existing independent image.
- Tag the immutable SHA used to build images, never a mutable branch head.
- Keep release tags, compose image references, and deployed digests aligned.
- Before production cutover: complete local Docker tests and any required browser validation, prepare/pull images, stop writers, take a fresh consistent rollback backup, migrate, start, and verify health.
- A release plan must include explicit rollback anchors. Never commit credentials or current production secrets into release documentation.
- Before rolling back to a binary that predates a newly seeded builtin tool, run the candidate image's idempotent rollback helper first. For IM recall this is `python -m app.scripts.rollback_im_recall`; only then start the older binary.
