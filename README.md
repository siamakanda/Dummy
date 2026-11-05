# Dummy — rate-deck automation

Automates "update the rate deck" for a VoIP carrier: read a rate-deck email, download the deck,
work out which trunk it belongs to, convert it to our template, validate, and create the deck on
the switch portal (dry-run by default).

See `AGENTS.md` (spec), `ARCHITECTURE.md` (context/architecture), `STATUS.md` (what's done / what's
left), and `DEPLOY.md` (VPS) for the full picture.

## Repository layout

    *.py                  flat modules (see ARCHITECTURE.md for the map) - kept at the repo root
    tests/                pytest suite + small fixtures (offline; no portal/email hit)
    docs/                 PROMPTS.md, final_review.html
    templates/            Jinja templates for the web admin (web.py)
    tools/                read-only helper scripts (network capture)
    samples/              large real sample decks (git-ignored; scale checks)
    archive/              retired v1 trunk-scraper package (git-ignored)
    inbox/                drop folder for the folder-first engine (git-ignored)
    data/                 runtime: SQLite DB, archives, logs, snapshots (git-ignored)
    .env / .env.example   secrets (ignored) and the committed template

## Setup

    python -m venv .venv
    .venv\Scripts\activate            # Windows (bash: source .venv/bin/activate)
    pip install -r requirements.txt
    playwright install chromium
    copy .env.example .env            # fill in PORTAL_USERNAME / PORTAL_PASSWORD

## M1 — trunk scraper (reads the switch portal)

    python scrape_trunks.py --discover       # save HTML + screenshots of key pages to data/snapshots/
    python scrape_trunks.py --all            # read carrier ids from /carriers.php, then sweep their trunks
    python scrape_trunks.py --ids 41         # one carrier (id from the carriers page)
    python scrape_trunks.py --ids 1-120      # an explicit range
    python scrape_trunks.py --all --headed   # watch the browser

Outputs `data/trunks.csv` (carrier + trunk names/ids, tech prefix, status, existing deck) and
`data/carriers.csv` (carrier id + name). Carriers with no trunk cards are skipped.

The parser is tuned against real pages (`parse.py` reads trunk **cards**: `.cpanel-head > strong`,
trunk id from the `edit=` link, tech prefix from the `prefix:` badge, deck from the `.cost-deck`
badge; carrier name from the page `<h1>`). To re-tune after a portal change, run `--discover`
and send `data/snapshots/*.html` (remove private data first).

## M2 — convert a carrier deck to our template (offline)

    python convert.py "carrier.csv" -o out.csv              # errors block output; warnings still write
    python convert.py "carrier.csv" -c trunk.yaml -o out.csv
    python convert.py "carrier.csv" --previous last_run.csv -o out.csv

Handles CSV / xlsx / zip, BOM, `#` comment lines (including a header written as a `#` line),
blank rows, and the per-carrier column aliases. `destination` is always `NANPA`;
`indeterminate_rate` falls back to `MAX(inter, intra)`; rates are kept as exact strings.

## M3 — which trunk? (store + matcher + review)

    python review.py --prefix 6060                        # match + interactive prompt
    python review.py --domain dialworld.com --prefix 1515

Keeps the trunk inventory and the audit trail in SQLite (`data/dummy.db`, created on first use; it
auto-loads `data/trunks.csv` when empty). The matcher maps the email sender domain → carrier, then
the tech prefix (exact) or product/trunk name (fuzzy) → the best trunk with a confidence score;
below **0.85** it asks a human via `review.py`.

## M4 — write to the portal (create deck + upload; dry-run first)

    python upload.py converted.csv --trunk "BTCL TNT TRUNK"                      # dry run: prints the plan
    python upload.py converted.csv --trunk "BTCL TNT TRUNK" --preview --headed   # screenshots, no writes
    python upload.py converted.csv --trunk "BTCL TNT TRUNK" --commit --headed    # actually writes

Creates a rate deck named `<Trunk> <effective date MM_DD_YYYY>` with **Use LRN for billing** ticked,
then uploads the converted CSV with **Replace all existing rates** ticked. Writes are gated:
`--commit` is allowed only for the development trunk `BTCL TNT TRUNK` (or a trunk already approved in
the store) — anything else is refused before the browser opens. Screenshots go to `data/snapshots/`.

## M5 — mailbox + fetcher (read the inbox)

Dummy polls its own IMAP inbox (`IMAP_HOST` / `IMAP_PORT` / `IMAP_USER` / `IMAP_PASSWORD` in `.env`),
unwraps the human "Forwarded message" layers down to the real carrier, and collects the deck —
attachment, zip, or a "Download Now" link (`requests` first, headless-browser fallback). Messages are
processed once, keyed by Message-ID. Tested entirely on `.eml` fixtures in `tests/fixtures/emails/`;
the test suite never touches a live mailbox.

## M6 — email understanding (extract facts)

    python email_llm.py tests/fixtures/emails/dialworld.eml      # rule-based (no key needed)
    python email_llm.py tests/fixtures/emails/nuso.eml --llm     # live LLM (needs LLM_API_KEY)

Turns an email into a validated `EmailFacts` object (carrier, trunk name, tech prefix, product,
effective date, billing increments, deck filename). A **rule-based** extractor always runs; when
`LLM_API_KEY` is set, an **OpenAI-compatible LLM (DeepSeek by default)** overlays the non-empty
fields, and any failure falls back to the rules. Strict JSON + pydantic validation; the LLM never
edits CSV rows. Provider is configurable: `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`.

## M7 — run it end to end

    python orchestrator.py --eml tests/fixtures/emails/nuso.eml   # process one email (dev, dry-run)
    python orchestrator.py                                       # one pass over the inbox (dry-run)
    python orchestrator.py --loop --interval 300                 # poll continuously
    python orchestrator.py --loop --commit                       # actually create + upload (gated)

Wires every stage: read the inbox → understand the email → fetch the deck → match the trunk (asking a
human when unsure) → convert → validate → dry-run report → (gated) create the deck + upload → notify
and record the audit trail (`data/archive/…`, `data/dummy.db`, `data/audit.jsonl`).

See **`DEPLOY.md`** for the VPS setup (Docker / systemd / cron, portal IP whitelist, backups) and the
`Dockerfile`.

## M8 — local directory input (+ trunk assignment)

    python orchestrator.py --source directory --dir "C:\path\to\decks" --trunk "BTCL TNT TRUNK"
    python orchestrator.py --source directory --dir "C:\path\to\decks" --commit

Reads deck files (`csv`/`xlsx`/`zip`) straight from a folder — no email needed — and runs the same
pipeline. Add a sidecar `<file>.csv.json` with `{trunk_name, tech_prefix, effective_date}` to supply
hints, or use `--trunk`. On commit the writer now **creates** the deck, **uploads** it, **assigns it
to the trunk**, and **verifies** the trunk's cost-deck badge; a failed upload/assign **deletes the
orphan deck**.

## M15 — housekeeping (cleanup + reuse)

    python cleanup.py --older-than-months 4                       # dry-run: list decks that would be deleted
    python cleanup.py --older-than-months 4 --commit --headed     # delete decks older than 4 months
    python cleanup.py --orphans --commit --headed                 # delete unassigned Dummy decks (never live ones)
    python upload.py converted.csv --trunk "BTCL TNT TRUNK" --reuse-deck --commit --headed
    python orchestrator.py --source directory --dir inbox --commit --reuse-deck   # pipeline reuse

Cleanup ages a deck by the date in its name, **never deletes a deck assigned to a trunk**, skips undated
decks, and only acts with `--commit`. `--orphans` sweeps only decks Dummy created but never assigned
(`<managed trunk> <date>`, unassigned, older than `--min-age-days`). `--reuse-deck` (on `upload.py`
**and** the orchestrator) uploads into the trunk's existing deck instead of creating a new one.

## Mailbox check (before wiring the inbox)

    python mailbox.py --check                 # connect with .env and report (no processing)
    python mailbox.py --check --folders       # also list folders / labels
    python mailbox.py --check --host imap.gmail.com --user you@gmail.com --password "<app password>"

Reads are plain IMAP-over-SSL (`IMAP_HOST` / `IMAP_PORT` / `IMAP_USER` / `IMAP_PASSWORD` / `IMAP_FOLDER`).
Reading **marks messages as read** by default; set `IMAP_MARK_SEEN="false"` to leave them unread (fetch
uses `BODY.PEEK`). Gmail needs an **App Password** (2-Step Verification on, IMAP enabled) — a normal
password will not work; a company/cPanel mailbox works with its mailbox password. Outlook / Microsoft 365
needs OAuth2 and is **not** supported.

Example (cPanel, e.g. `instacall.us`):

    IMAP_HOST="instacall.us"   IMAP_PORT="993"   IMAP_USER="siam@instacall.us"

Email notifications through the same server use SMTP port **465** (implicit SSL) — Dummy detects 465 and
uses `SMTP_SSL` automatically (any other port uses STARTTLS).

## Unknown layouts (LLM mapping proposal)

When a carrier file's columns can't be mapped, Dummy asks the LLM (only if `LLM_API_KEY` is set) to
propose a mapping and queues the item with a ready-made suggestion:

    python review.py list                       # shows the mapping item + suggested_aliases
    python review.py apply-mapping <id>         # accept the suggestion -> saved as header_aliases
    python review.py apply-mapping <id> --aliases '{"prefix": ["Dialcode"]}'

Then re-run the source (or the daemon) to convert + upload. Without an `LLM_API_KEY` the item is still
queued, just with no suggestion.

## Engine (folder-first core, Windows-local)

A narrower, operator-friendly flow over the same code (branch `engine`): drop decks into `inbox/`, map
each file to a trunk, convert, and push.

    python engine.py sync                 # read trunks from the switch -> SQLite (+ data/trunks.csv)
    python engine.py scan                 # scan inbox/ -> one job per deck (with suggestions)
    python engine.py list --trunks        # jobs + the switch trunks
    python engine.py map 1 "BTCL TNT TRUNK"
    python engine.py run                  # convert + validate mapped jobs (dry run)
    python engine.py push                 # dry-run the upload; add --commit (dev trunk only)

Prefer a UI? The local dashboard (bound to 127.0.0.1, **no login**) wraps the same steps:

    uvicorn engine_web:app --host 127.0.0.1 --port 8090

Map file -> trunk in the browser, click Convert, then Dry-run / Push. The UI never touches the portal
itself - the shared `engine` functions do, and a real write still needs Push on an allowed trunk.

## Tests

    pytest        # parser + converter tests (no browser / portal / email needed)
