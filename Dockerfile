FROM python:3.11-slim

WORKDIR /app

# pg_dump for the nightly backup step (scripts/backup.sh, run from the
# maintenance container). The client major must match the postgres server
# (pgvector/pgvector:pg16) — bump postgresql-client-NN when the server bumps.
# Debian's default client is too old, so pull v16 from the pgdg apt repo.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
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
# fastmcp/asyncpg/embedding deps; the thin-client base install never needs them.
COPY . .
RUN pip install --no-cache-dir .[server]

# The library volume is bind-mounted here inside the container. This overrides
# config.yaml's library_path (the host-side path used by scripts/backup.sh) so
# document ingest writes to the mounted path instead of the unmounted host path.
ENV OPENBRAIN_LIBRARY_PATH=/data/openbrain/library

# Run the MCP server
CMD ["python", "-m", "openbrain.server"]
