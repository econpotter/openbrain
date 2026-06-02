# Open Brain

A personal AI memory and knowledge base for researchers. One Postgres database,
one MCP server, any AI can plug in. No middleware, no SaaS, no subscriptions
beyond the AI you already use.

You interact with your memory entirely through Claude.ai or Gemini — the AI
mediates all reads and writes. You never touch the database directly.

## What it does

- **Capture thoughts** — store notes, memories, and conversation summaries with
  AI-extracted metadata (topics, people, projects)
- **Semantic search** — find anything by meaning, not just keywords
- **Library** — ingest PDFs and documents; search across them by topic and year
- **Entities** — track people, projects, and relationships between them
- **Daily briefing** — surface active projects, recent thoughts, and follow-ups
  each morning

Everything is stored in a local Postgres database. The MCP server runs on your
machine and is accessible remotely from any client you connect.

## Architecture

```
Claude.ai / Gemini
    └── MCP server  (Python/FastMCP, port 8000)
           └── brain.py  (core logic)
                  ├── Postgres + pgvector  (structured records + vector search)
                  └── Embedding provider    (Voyage AI, OpenRouter, or LiteLLM)

python -m openbrain.cli  (local CLI for adding PDFs and documents)
```

## Prerequisites

- Linux machine (the server that will host the brain)
- Python 3.11+
- PostgreSQL 15+ with the [pgvector](https://github.com/pgvector/pgvector) extension
- Network access from your client machines to the server on port 8000 for
  direct HTTP, or port 8443 when using the bundled Caddy TLS proxy

**Embeddings** are used to power semantic search. The default provider is Voyage
AI. OpenRouter and an OpenAI-compatible LiteLLM gateway are also supported.

| Provider | Default model | Configuration |
|----------|---------------|---------------|
| Voyage AI (default) | `voyage-large-2` | `VOYAGE_API_KEY` |
| OpenRouter | Configured in `config.yaml` | `OPENROUTER_API_KEY` |
| LiteLLM | Configured in `config.yaml` | `LITELLM_API_KEY` and top-level `litellm_url` |

The initial schema uses 1536-dimensional vectors to match `voyage-large-2`.
Changing to a model with a different output dimension requires a schema change
and re-embedding existing records.

Nightly maintenance uses a separate chat model configured in `config.yaml`.
That model consolidates recent raw thoughts into durable synthesis memories and
does not need to match the embedding model.

### Install PostgreSQL and pgvector

```bash
sudo apt install postgresql postgresql-contrib
sudo apt install postgresql-16-pgvector   # adjust version to match your postgres
sudo systemctl enable --now postgresql
```

Create the database:

```bash
sudo -u postgres createuser --superuser $USER
createdb openbrain
```

## Deployment

### 1. Clone the repository

```bash
git clone <repo-url> ~/openbrain
cd ~/openbrain
```

### 2. Run the installer

```bash
bash scripts/install.sh
```

The installer will:
1. Create a Python virtual environment and install dependencies
2. Create `.env` from `.env.example` and **pause** — you must fill in credentials
3. Install the core database schema
4. Install and start the `openbrain-mcp` systemd service

### 3. Fill in credentials

Open `.env` and set:

```
DATABASE_URL=postgresql://localhost/openbrain
VOYAGE_API_KEY=your_key_here
MCP_ACCESS_KEY=your_mcp_key_here
```

Then re-run the installer to continue:

```bash
bash scripts/install.sh
```

### 4. Verify the service is running

```bash
sudo systemctl status openbrain-mcp
```

### 4b. Set up HTTPS with Caddy (Docker deployment on VPN-only hosts)

The repository-level `docker-compose.yaml` runs the MCP server, Postgres, and
Caddy together, plus a maintenance container that performs nightly thought
consolidation. Caddy terminates TLS on port 8443 and proxies to the MCP server
on the Docker network. This is an alternative to running the MCP server through
the systemd service installed above.

Set your hostname in `.env` next to `docker-compose.yaml`:

```
CADDY_HOSTNAME=potter.nicholas-fuji.nord
```

Then bring up (or restart) the stack:

```bash
docker compose up -d
```

On first start with a new PostgreSQL volume, Docker installs
`schemas/core/*.sql` automatically. To test the schema without retaining a
database volume or starting the full stack:

```bash
bash openbrain/scripts/run-tests.sh --postgres
```

This runs the Python-level tests, then starts an ephemeral PostgreSQL + pgvector
container, checks the schema and duplicate-merge behavior, and removes the
container afterward. Use `bash openbrain/scripts/run-tests.sh` when you only
want the Python-level checks.

Caddy uses `tls internal` — it generates its own CA and issues a cert for the
hostname. You need to trust that CA cert once on each client machine:

> **NordVPN meshnet users:** after bringing up the stack, also follow the
> firewall step in [Troubleshooting → NordVPN meshnet](#nordvpn-meshnet-port-8443-times-out)
> or port 8443 will silently time out despite appearing open.

```bash
# Extract the root cert from the running container
docker exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt

# Copy caddy-root.crt to your client machine, then:

# Linux
sudo cp caddy-root.crt /usr/local/share/ca-certificates/caddy-root.crt
sudo update-ca-certificates

# macOS
sudo security add-trusted-cert -d -r trustRoot \
    -k /Library/Keychains/System.keychain caddy-root.crt
```

If your server is publicly reachable, remove the `tls internal` line from
`Caddyfile` — Caddy will obtain a real Let's Encrypt certificate automatically.

### 5. Connect your AI clients

Once the service is running, connect whichever clients you use.

#### Claude.ai (web)

**Settings → Connectors → Add MCP server**

URL: `https://<your-hostname>:8443/mcp`

Claude.ai requires HTTPS. Set up Caddy first (step 4b) if your host is VPN-only.
If you set `MCP_ACCESS_KEY`, configure the connector to send it as a Bearer token.

#### Claude Code (CLI)

If you set `MCP_ACCESS_KEY`, pass the same value as a Bearer token:

```bash
claude mcp add --transport http \
  --header "Authorization: Bearer <your-mcp-access-key>" \
  openbrain https://<your-hostname>:8443/mcp
```

To add the server through JSON configuration instead:

```json
{
  "mcpServers": {
    "openbrain": {
      "type": "http",
      "url": "https://<your-hostname>:8443/mcp",
      "headers": {
        "Authorization": "Bearer <your-mcp-access-key>"
      }
    }
  }
}
```

Then verify the connection:

```bash
claude mcp list
```

#### Project instruction (Claude.ai)

To make Claude automatically preserve memories at the end of conversations,
add this to your Claude.ai project instructions:

```
At the end of our conversation, or when I say "remember this", call
capture_thought to store a distilled summary of any facts, decisions, or
insights worth preserving. Extract people, projects, and topics from the
content and include them in metadata. Link to relevant library entries
where applicable.
```

## Manual deployment (without the installer)

If you prefer to run steps individually:

```bash
# 1. Create venv and install deps
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env with your DATABASE_URL and the key for your configured embed provider

# 3. Install the core database schema
bash scripts/setup_db.sh

# 4. Start the MCP server
MCP_TRANSPORT=http python -m openbrain.server
```

## Changing the embedding provider

Embedding provider and model selection live in `config.yaml`. API keys stay in
`.env`. The default is:

```yaml
# config.yaml
litellm_url: http://litellm:4000

embed:
  provider: voyage
  model: voyage-large-2
```

```dotenv
# .env
VOYAGE_API_KEY=your_key_here
```

To route embeddings through OpenRouter or LiteLLM, update `embed.provider`,
`embed.model`, and, for LiteLLM, top-level `litellm_url`. See
`docs/design/model-config.md`.

If you switch models after storing data, existing vectors and new vectors
will be from different models and semantic search results will be unreliable.
If the output dimension also changes, update the schema before re-embedding.

## Nightly maintenance

The Compose stack includes an `openbrain-maintenance` container. It runs
`scripts/openbrain-maintenance.py --loop`, sleeps until `maintenance.run_at`,
then consolidates recent unarchived thoughts with the configured maintenance
model.

```yaml
litellm_url: http://litellm:4000

maintenance:
  provider: litellm
  model: <local-or-remote-chat-model>
  run_at: "02:00"
  lookback_hours: 24
  batch_size: 50
```

Run one maintenance pass manually from the stack with:

```bash
docker compose run --rm maintenance python scripts/openbrain-maintenance.py
```

## Backup and restore

The installer sets up a daily systemd timer that backs up both the database and
library files automatically. Backups are kept for 30 days by default.

### Run a backup manually

```bash
bash scripts/backup.sh
```

Backups are written to `BACKUP_DIR` (default `~/backups/openbrain`):

```
openbrain_20260324_020000.sql.gz   ← database dump
library_20260324_020000.tar.gz     ← library files
```

### Check backup timer status

```bash
systemctl status openbrain-backup.timer
systemctl list-timers openbrain-backup.timer
```

### Restore the database

```bash
# Drop and recreate the database
dropdb openbrain && createdb openbrain

# Restore from a dump
gunzip -c ~/backups/openbrain/openbrain_<timestamp>.sql.gz | psql $DATABASE_URL
```

### Restore library files

```bash
tar -xzf ~/backups/openbrain/library_<timestamp>.tar.gz -C ~/
```

### Configure backup retention

In `.env`:

```
BACKUP_DIR=~/backups/openbrain
BACKUP_KEEP_DAYS=30
```

## Development

Run the server locally over stdio (no HTTP, useful for testing with the MCP
inspector or a local Claude Code session):

```bash
source .venv/bin/activate
python -m openbrain.server   # MCP_TRANSPORT defaults to stdio
```

Ingest a document:

```bash
python -m openbrain.cli paper.pdf
python -m openbrain.cli papers/              # whole directory
python -m openbrain.cli paper.pdf --force    # re-ingest a duplicate
```

## Implementation status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Core capture & recall (`capture_thought`, `search_thoughts`, `browse_recent`, `get_stats`) | Complete |
| 2 | Library (`ingest_document`, `ingest_directory`, `search_library`, `update_library`, `update_status`) | Complete |
| 3 | Entities (`add_person`, `add_project`, `list_people`, `list_projects`, `update_person`, `update_project`, `search_people`, `search_projects`, `search_by_entity`) | Complete |
| 4 | Cross-table intelligence (`search_brain`, `get_briefing`) | Complete |
| 5 | Hardening (systemd, daily backups, logging) | Complete |

## Troubleshooting

See `../README.md` for infrastructure-level issues (NordVPN meshnet, Caddy TLS, Docker networking).

### NordVPN meshnet: ports time out

**Symptom:** `curl https://<hostname>:6666/` (or 8443) hangs. `tcpdump -i nordlynx port 6666` shows SYN packets arriving from the client but no SYN-ACK is sent back. SSH works fine. ufw is inactive; iptables INPUT/FORWARD rules have no effect.

**Root cause (3 layers):**

1. **NordVPN uses nftables, not iptables.** Its `table inet nordvpn` registers a FORWARD chain at `priority filter` (0) with `policy drop`. iptables rules (also at priority 0) lose the race — NordVPN's chain runs first and drops forwarded meshnet traffic before Docker's accept rules can fire. Inserting iptables ACCEPT rules or nftables rules at `priority filter - 1` does not help: in nftables, `accept` from one base chain does not prevent subsequent chains at higher priority from running. Only `drop` is final across the pipeline.

2. **Docker's PREROUTING DNAT does not fire for nordlynx traffic.** Docker's nat PREROUTING rule uses `ADDRTYPE match dst-type LOCAL`, which does not match the nordlynx interface address on some kernels. An explicit DNAT rule is required.

3. **WireGuard MTU mismatch.** nordlynx MTU is ~1420 bytes. Docker containers use MSS 1460, producing ~1500-byte TCP segments that exceed the tunnel MTU and are silently dropped on the server→client path.

**Fix — run on the server (fuji):**

First, find the Caddy container IP (may change if Docker recreates the network):
```bash
docker inspect caddy | grep '"IPAddress"'
# e.g. 172.18.0.6
```

**Step 1 — explicit PREROUTING DNAT for nordlynx traffic:**
```bash
sudo iptables -t nat -I PREROUTING 1 -i nordlynx -p tcp --dport 6666 -j DNAT --to-destination 172.18.0.6:6666
sudo iptables -t nat -I PREROUTING 1 -i nordlynx -p tcp --dport 8443 -j DNAT --to-destination 172.18.0.6:8443
```

**Step 2 — insert accept rules inside NordVPN's own nftables chains:**

Replace `172.18.0.0/16` with your Docker bridge subnet if it differs (`docker network inspect bridge | grep Subnet`).
```bash
sudo nft 'insert rule inet nordvpn mesh_peer_to_internet ip saddr 100.64.0.0/10 ip daddr 172.18.0.0/16 tcp dport { 6666, 8443 } accept'
sudo nft 'insert rule inet nordvpn internet_to_mesh_peer ip daddr 100.64.0.0/10 ip saddr 172.18.0.0/16 tcp sport { 6666, 8443 } accept'
```

**Step 3 — clamp TCP MSS for WireGuard MTU:**
```bash
sudo iptables -t mangle -A FORWARD -i nordlynx -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1360
```

**Step 4 — persist iptables rules (DNAT + MSS clamp survive reboots):**
```bash
sudo iptables-save | sudo tee /etc/iptables/rules.v4
```

**Step 5 — persist NordVPN nftables rules across reconnects:**

NordVPN wipes `table inet nordvpn` whenever it reconnects, so the Step 2 rules must be re-applied automatically. Use the installer from the repo root:
```bash
sudo bash ../scripts/install-meshnet-docker-nft.sh
```

Override defaults if needed:
```bash
sudo MESHNET_PEER_IP=100.64.0.0/10 MESHNET_DOCKER_PORTS=6666,8443 bash ../scripts/install-meshnet-docker-nft.sh
```

See `../scripts/README-meshnet-nft.md` for details.

**Step 6 — trust Caddy's CA cert on the client (reason):**

Caddy uses `tls internal` (self-signed CA). The client must trust it or TLS handshakes fail silently.
```bash
# On fuji:
docker cp caddy:/data/caddy/pki/authorities/local/root.crt /tmp/caddy-fuji.crt
# Copy caddy-fuji.crt to reason, then on reason:
sudo cp caddy-fuji.crt /usr/local/share/ca-certificates/caddy-fuji.crt
sudo update-ca-certificates
```

**Verify:**
```bash
curl -s https://potter.nicholas-fuji.nord:6666/health
# {"status":"healthy","database":"connected"}
curl -s https://potter.nicholas-fuji.nord:8443/mcp/
```

## Repository structure

```
openbrain/
  brain.py           Core logic: embed, store, retrieve
  server.py          FastMCP server entry point
  cli.py             Standalone CLI for ingesting documents
schemas/core/        Clean install-time SQL baseline, applied in numeric order
tools/               MCP tool implementations, one file per tool group
scripts/
  setup_db.sh                 Install the core database schema
  install.sh                  Full install: venv, .env, DB, systemd, backup timer
  backup.sh                   Back up database and library files
  openbrain-mcp.service       systemd unit template for MCP server
  openbrain-backup.service    systemd unit template for backup job
  openbrain-backup.timer      systemd timer (runs backup daily)
```
