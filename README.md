# Dummy — carrier rate-deck engine

Dummy automates the recurring chore of **"update the rate deck"** for a VoIP carrier. A carrier sends
a rate deck (CSV / XLSX / ZIP); we map it to a trunk on the switch, convert it to our fixed template,
validate it, **upload** it as a rate deck (with **"Use LRN for billing"** ticked), then **assign** that
deck to the trunk — safely (dry-run by default, allowed trunks only).

The primary, day-to-day experience is the **folder-first engine + local dashboard** (Windows): drop
decks in a folder, map each file to a trunk, convert, upload the deck, then assign it. The original
**email pipeline** is still included for unattended intake (see [Email pipeline](#email-pipeline-included)).

> The portal is live billing infrastructure. Nothing is written unless you **Upload** / **Assign**
> (or pass `--commit`), and only for the development trunk or a trunk a human approved.

## Quick start (Windows)

1. Install Python 3.11+, then once:
   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   playwright install chromium
   copy .env.example .env        :: fill in PORTAL_USERNAME / PORTAL_PASSWORD
   ```
2. **Double-click `start.bat`.** It opens the dashboard in your browser at <http://127.0.0.1:8090> and
   keeps the engine running in that terminal window. Keep it open; Ctrl+C stops it.

Every option lives in the dashboard — the terminal is just the engine's console.

## The daily workflow

1. **Drop** the carrier's rate-deck files (`csv` / `xlsx` / `zip`) into the **`inbox/`** folder.
2. **Scan inbox/** — Dummy reads each file and lists it as a job with *suggested* trunks.
3. **Map** each file to the right trunk (pick from the full trunk list; click a suggestion to pre-select).
4. **Convert** — turns the carrier layout into our template and validates it (row count, prefixes, rates).
5. **Upload to deck** — creates the rate deck on the switch and uploads the rates (not assigned yet).
6. **Assign to trunk** — wires that uploaded deck to the trunk and verifies it.

Every action reports back: a live console overlay streams what Dummy is doing, and a colour-coded banner
summarises the result. Real writes happen only on **Upload** / **Assign** (or `--commit`) for an allowed trunk.

## Dashboard

- **Stat cards** — deck files · unmapped · mapped · ready to upload · uploaded · assigned · problems.
- **Filter bar** — live search (file / trunk / status) + a status dropdown with an “N of M” count.
- **Table** — filename, effective date, suggested trunks, a grouped trunk dropdown, a colour status chip,
  and **per-row actions** (Convert · Upload · Assign · assigned).
- **Approve trunk** — if the mapped trunk isn't approved yet, the row offers a one-click **Approve trunk**
  button (same effect as `review.py approve-trunk`); Assign then unlocks.
- **Deck reference + Verify** — an uploaded/assigned deck shows its id, name and creation time; **Verify**
  re-reads the switch to confirm the deck still exists and the name matches what we recorded.
- **Effective date is never guessed** — if the email/file has no reliable date, the row shows **date
  needed** with a date input and a filename hint; Upload stays blocked until you set it.
- **Redo** — an uploaded (not yet assigned) deck can be deleted and the job reset (to fix a wrong name or
  date), then uploaded again. Assigned decks are never deleted.
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
python engine.py upload               :: step 1: create the deck + upload the CSV (add --commit)
python engine.py assign               :: step 2: assign an uploaded deck to its trunk (add --commit)
```

`upload` creates the deck and uploads the rates; `assign` wires the deck to the trunk and verifies it.
Each is dry-run unless `--commit`; a failed upload deletes its orphan deck.

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

- **Dry-run by default** — writes require **Upload** / **Assign** (dashboard) or `--commit` (CLI).
- **Assign is gated, Upload isn't** — `Upload` only creates a deck (no trunk/billing change), so any trunk is fine; `Assign` changes billing and is limited to `BTCL TNT TRUNK` or a human-approved trunk.
- **Never guess** — an ambiguous trunk goes to a human, not a guess. Declared **trunk aliases** resolve
  known naming variants deterministically.
- **No orphans from uploading** — a failed upload deletes its half-created deck; an uploaded-but-never-assigned deck is swept by `cleanup.py --orphans`.
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
