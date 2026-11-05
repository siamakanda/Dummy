# Deploying Dummy on a VPS

Dummy polls its own inbox, converts each rate deck, and (only when allowed) creates/uploads the
deck on the SBC portal. **Dry-run is the default** — nothing is written to the portal without
`--commit` *and* a trunk that is either the development trunk (`BTCL TNT TRUNK`) or human-approved.

## 1. Before you start

- A small Linux box is plenty (**2 vCPU / 4 GB RAM**); the heaviest step is ~240k-row decks, which
  convert in ~1 s.
- A mailbox Dummy can log into over **IMAP** (dedicated address recommended).
- The portal login (ideally a **dedicated low-privilege user** — Dummy can write billing data).
- **IP restriction?** If the switch only accepts certain IPs, whitelist the VPS's static IP first
  (you answered "not IP-restricted", so this is a no-op unless that changes).

## 2. Configure `.env`

Copy `.env.example` → `.env` and fill in:

```
PORTAL_BASE_URL="https://switchportal.instacall.digital"
PORTAL_USERNAME="..."
PORTAL_PASSWORD="..."
IMAP_HOST="imap.example.com"
IMAP_PORT="993"
IMAP_USER="dummy@example.com"
IMAP_PASSWORD="..."
# Optional:
# IMAP_FOLDER="INBOX"
# EFFECTIVE_DATE_FORMAT="%Y-%m-%d"
# LLM_BASE_URL="https://api.deepseek.com"
# LLM_API_KEY="sk-..."
# LLM_MODEL="deepseek-chat"
```

Keep `.env` **out of git** and owner-only (`chmod 600 .env`). `data/` (archive, `dummy.db`,
`audit.jsonl`) is git-ignored.

## 3. Run it

### Option A — Docker (recommended)

```bash
docker build -t dummy .
docker run -d --name dummy --restart unless-stopped \
  --env-file .env \
  -v "$PWD/data:/app/data" \
  dummy                      # runs: python orchestrator.py --loop --interval 300
```

Start in **dry-run** first (the default). When you're ready to let it write, add `--commit`:

```bash
docker run -d --name dummy --restart unless-stopped --env-file .env \
  -v "$PWD/data:/app/data" dummy \
  python orchestrator.py --loop --interval 300 --commit
```

### Option B — systemd (no Docker)

`/etc/systemd/system/dummy.service`:

```ini
[Unit]
Description=Dummy rate-deck agent
After=network-online.target

[Service]
WorkingDirectory=/opt/dummy
ExecStart=/opt/dummy/.venv/bin/python orchestrator.py --loop --interval 300
Restart=always
RestartSec=15
EnvironmentFile=/opt/dummy/.env

[Install]
WantedBy=multi-user.target
```

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && playwright install --with-deps chromium
sudo systemctl enable --now dummy
```

### Option C — cron (one-shot)

```
*/5 * * * * cd /opt/dummy && .venv/bin/python orchestrator.py 2>&1 >> data/cron.log
```

(No `--loop`; each run processes the unseen inbox once. Add `--commit` when you're ready.)

## 4. Day-to-day

- **First run per trunk:** the matcher asks a human (`review.py`) because the trunk isn't yet in
  `approved_trunks`. Once approved, later runs auto-proceed.
- **Audit trail** (your evidence in a rate dispute): `data/archive/<date>/<message-id>/` holds the
  original `.eml`, the original deck, and the converted CSV; `data/dummy.db` holds runs/decisions;
  `data/audit.jsonl` is a line-per-run log. **Back these up.**
- **Test trunk:** uploads during development are limited to `BTCL TNT TRUNK`.
- **Idempotency:** messages are processed once, keyed by `Message-ID`, so restarts don't double-apply.

## 5. Operations

- Logs: `docker logs -f dummy` (or `journalctl -u dummy -f`).
- Stop the loop: `docker stop dummy` / `systemctl stop dummy`.
- The portal session is cached in `.state/state.json`; delete it to force a fresh login.

## 6. Web admin (human-in-the-loop)

The dashboard reads the same `data/dummy.db` and lets a human resolve pending items; it **never**
touches the portal. The **daemon** applies resolved items on its next pass — so run the orchestrator
with `--commit`:

```bash
docker compose up -d --build          # orchestrator (dry-run) + web + caddy
# To let the daemon apply resolved items, add --commit to its command in docker-compose.yml.
```

`.env` needs:

```
ADMIN_USER="admin"
ADMIN_PASSWORD="a-strong-password"
SESSION_SECRET="a-long-random-string"      # signs the session cookie
DUMMY_DOMAIN="admin.example.com"           # Caddy issues TLS automatically; default "localhost"
```

Access:
- **Domain + TLS:** point `DUMMY_DOMAIN` at the server and open `https://admin.example.com`.
- **No public domain:** keep `DUMMY_DOMAIN=localhost` and tunnel:
  `ssh -L 8080:localhost:8080 user@vps`, then open `http://localhost:8080`.

Flow: the daemon runs `--loop --commit`; unresolved/approval/warning items appear in the dashboard;
a human resolves them; the daemon commits on the next poll (create → upload → assign → verify).

