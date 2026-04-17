# Minimal sandbox base for running user-uploaded CLI binaries.
# No extra packages. The uploaded binary is bind-mounted at /binary read-only
# and invoked directly. Run as `nobody` with cap-drop ALL and no-new-privileges.
FROM debian:bookworm-slim

# Upgrade packages so we pick up whatever base-image security fixes Debian has
# shipped since the last :stable rebuild.
RUN apt-get update \
  && apt-get upgrade -y --no-install-recommends \
  && rm -rf /var/lib/apt/lists/*

# nobody (65534) is present in debian:slim; assert it exists so the image
# fails fast if Debian ever changes that.
RUN id -u nobody >/dev/null

# Default invocation is overridden by `docker run`; this CMD makes a bare
# `docker run <image>` exit cleanly rather than drop into a shell we don't
# want a sandboxed binary to inherit.
CMD ["/bin/true"]
