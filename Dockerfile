# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

# pg_dump for the nightly backup step (scripts/backup.sh, run from the
# maintenance container). The client major must match the postgres server
# (pgvector/pgvector:pg16) — bump postgresql-client-NN when the server bumps.
# Debian's default client is too old, so pull v16 from the pgdg apt repo.
# git is kept (not purged) because pip needs it at build time to install the
# `citer` git dependency declared in the [server] extra.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg git \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# Copy the application and install the server stack. The `[server]` extra pulls
# fastmcp/asyncpg/embedding deps plus the `citer` git dependency; the thin-client
# base install never needs them.
COPY . .
# `citer` lives in a private GitHub repo, so pip needs credentials to fetch it.
# Pass a GitHub token as a BuildKit secret (never baked into an image layer):
#   docker build --secret id=github_token,env=GITHUB_TOKEN ...
# When no secret is provided (public repo or a pre-authenticated builder), the
# install proceeds unchanged. The temporary credential is written to and removed
# from /root/.gitconfig within this single layer so the token never persists.
RUN --mount=type=secret,id=github_token \
    if [ -s /run/secrets/github_token ]; then \
        git config --global \
            url."https://$(cat /run/secrets/github_token)@github.com/".insteadOf \
            "https://github.com/"; \
    fi; \
    pip install --no-cache-dir .[server]; \
    rm -f /root/.gitconfig

# The library volume is bind-mounted here inside the container. This overrides
# config.yaml's library_path (the host-side path used by scripts/backup.sh) so
# document ingest writes to the mounted path instead of the unmounted host path.
ENV OPENBRAIN_LIBRARY_PATH=/data/openbrain/library

# Run the MCP server
CMD ["python", "-m", "openbrain.server"]
