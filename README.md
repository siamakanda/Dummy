# Dummy — carrier rate-deck engine

Dummy automates the recurring chore of **"update the rate deck"** for a VoIP carrier. A carrier sends
a rate deck (CSV / XLSX / ZIP); we map it to a trunk on the switch, convert it to our fixed template,
validate it, and push it to the portal — with **"Use LRN for billing"** ticked — safely (dry-run by
default, allowed trunks only).

The primary, day-to-day experience is the **folder-first engine + local dashboard** (Windows): drop
decks in a folder, map each file to a trunk, convert, and push. The original **email pipeline** is still
included for unattended intake (see [Email pipeline](#email-pipeline-included)).

> The portal is live billing infrastructure. Nothing is written unless you press **Push** / pass
> `--commit`, and only for the development trunk or a trunk a human approved.

## Quick start (Windows)

1. Install Python 3.11+, then once:
   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   playwright install chromium
   copy .env.example .env        :: fill in PORTAL_USERNAME / PORTAL_PASSWORD
   ```
2. **Double-click `start.bat`.** A small terminal menu appears; choose **1) Start dashboard** (or press
   Enter). Your browser opens the dashboard at <http://127.0.0.1:8090>. Keep the window open; Ctrl+C stops it.

The menu also offers refresh-trunks / scan-inbox / convert / status. Power users can run one directly:
`python launcher.py status|sync|scan|convert`.

## The daily workflow

1. **Drop** the carrier's rate-deck files (`csv` / `xlsx` / `zip`) into the **`inbox/`** folder.
2. **Scan inbox/** — Dummy reads each file and lists it as a job with *suggested* trunks.
3. **Map** each file to the right trunk (pick from the full trunk list; click a suggestion to pre-select).
4. **Convert** — turns the carrier layout into our template and validates it (row count, prefixes, rates).
5. **Dry-run**, review, then **Push** — create the deck, upload the rates, assign it to the trunk, verify.

Every action reports back: a live console overlay streams what Dummy is doing, and a colour-coded banner
summarises the result. A real write happens only on **Push** (or `--commit`) for an allowed trunk.

## Dashboard

- **Six stat cards** — deck files · unmapped · mapped · ready to push · uploaded · problems.
- **Filter bar** — live search (file / trunk / status) + a status dropdown with an “N of M” count.
- **Table** — filename, effective date, suggested trunks, a grouped trunk dropdown, a colour status chip,
  and **per-row actions** (Convert · Dry-run · Push · done).
- **Live console overlay** — streams the engine’s log lines while a background job runs.
- **Flash messages** — success / info / warning / error, with auto-dismiss.
- Localhost only, **no login** (CSRF-protected), with a favicon.

Run it directly if you prefer:
```bat
uvicorn engine_web:app --host 127.0.0.1 --port 8090
```

## Engine CLI

```bat
python engine.py sync                 :: read trunks from the switch -> SQLite (+ data/trunks.csv)
python engine.py scan                 :: scan inbox/ -> one job per deck (with suggestions)
python engine.py list --trunks        :: jobs + the switch trunks
python engine.py map 1 "BTCL TNT TRUNK"
python engine.py run                  :: convert + validate mapped jobs (dry run)
python engine.py push                 :: dry-run the upload; add --commit to write
```

`push` does **create → upload → assign → verify** and deletes the orphan deck if any step fails.

## Configuration (`.env`)

| Key | Purpose |
|---|---|
| `PORTAL_BASE_URL`, `PORTAL_USERNAME`, `PORTAL_PASSWORD` | switch portal (login + writes) |
| `DUMMY_PORT` | dashboard port (default `8090`) |
| `INBOX_DIR` | engine drop folder (default `./inbox`) |
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` | email intake |
| `IMAP_MARK_SEEN` | `false` leaves mail unread (`BODY.PEEK`) |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | optional LLM (DeepSeek by default) |
| `ADMIN_USER`, `ADMIN_PASSWORD`, `SESSION_SECRET` | VPS web admin |
| `SMTP_*` | email notifications (port 465 uses implicit SSL) |
| `LOG_LEVEL`, `LOG_FILE` | logging |

Secrets live only in `.env` (git-ignored). `.env.example` is the committed template.

## Safety model

- **Dry-run by default** — writes require `--commit` (CLI) or **Push** (dashboard).
- **Allowed trunks only** — the development trunk `BTCL TNT TRUNK`, or a trunk a human approved.
- **Never guess** — an ambiguous trunk goes to a human, not a guess. Declared **trunk aliases** resolve
  known naming variants deterministically.
- **No orphans** — a failed create/upload/assign deletes the half-made deck.
- **Audit trail** — `data/dummy.db`, `data/audit.jsonl`, archived originals under `data/archive/`.

## Repository layout

    *.py                  flat modules (see ARCHITECTURE.md for the map)
    tests/                pytest suite + small offline fixtures (no portal/email/browser)
    templates/            Jinja templates (dashboard + web admin)
    docs/                 PROMPTS.md
    samples/              large real sample decks (git-ignored; manual scale checks)
    inbox/                drop folder for the engine (git-ignored)
    data/                 runtime: SQLite DB, archives, logs (git-ignored)
    .env / .env.example   secrets (ignored) and the committed template

## Tests

```bat
python -m pytest -q        :: 172 tests, fully offline
```

## Email pipeline (included)

Unattended intake over IMAP: read the forwarded email, fetch the deck (attachment / zip / link), match the
trunk, convert, validate, dry-run, then (gated) create + upload + assign, with notifications and audit.

```bat
python mailbox.py --check                 :: verify IMAP credentials (no processing)
python orchestrator.py --eml tests/fixtures/emails/nuso.eml   :: one email (dry-run)
python orchestrator.py --loop --interval 300                  :: poll continuously
python orchestrator.py --loop --commit                        :: write (gated)
```

See **`DEPLOY.md`** for the VPS setup (Docker / systemd / cron) and `web.py` for the authenticated admin
over the pending queue.

## Docs

- **`AGENTS.md`** — the authoritative spec (business rules, template, milestones).
- **`ARCHITECTURE.md`** — how the pieces fit together, the current implementation map.
- **`STATUS.md`** — what's done, verified, and what's left.
- **`DEPLOY.md`** — server deployment.
