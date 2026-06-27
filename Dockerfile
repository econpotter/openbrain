FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# The library volume is bind-mounted here inside the container. This overrides
# config.yaml's library_path (the host-side path used by scripts/backup.sh) so
# document ingest writes to the mounted path instead of the unmounted host path.
ENV OPENBRAIN_LIBRARY_PATH=/data/openbrain/library

# Run the MCP server
CMD ["python", "-m", "openbrain.server"]
