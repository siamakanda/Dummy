# Dummy — Architecture & Working Context

> Living document. Read alongside `AGENTS.md` (the authoritative spec) before any task.
> Last verified against the repo: 2026-11-04. Milestone status + what's left: see `STATUS.md`.

This file is the single place to understand **what Dummy is, what is built, what the rules
are, and how the pieces fit together**. When `AGENTS.md` and this file disagree, `AGENTS.md`
wins and you should ask before choosing (per AGENTS.md §8b).

---

## 1. What Dummy is

A VoIP carrier (Instacall) is interconnected with many carriers worldwide. Each carrier emails
us a **rate deck** (CSV, sometimes zipped, sometimes behind a download link), weekly or monthly.
Today a human opens the switch portal and "updates the rate deck" for the matching trunk.

**Dummy** automates that human task end to end:

receive email → download the deck → decide **which trunk** → convert to our template → validate
→ create a rate deck on the portal (with **"Use LRN for billing"** ticked) → audit log.

The portal is **live billing infrastructure**. Every write action is **dry-run by default** and
only happens with an explicit `--commit` (AGENTS.md §2).

Development test trunk on the switch: **`BTCL TNT TRUNK`** — never test uploads on any other trunk.

---

## 2. Roles & vocabulary

| Term | Meaning |
|---|---|
| **Dummy** | The Python agent (this project). |
| **Carrier** | Interconnected carrier that sends us rate decks. |
| **Trunk** | A carrier connection configured on our SBC portal; the deck belongs to one trunk. |
| **Rate deck** | Per-trunk CSV of prefixes and rates. |
| **Template** | Our fixed output format (section 5 below). |
| **Tech prefix** | A routing prefix a carrier quotes in the email (e.g. `6060`) used to match a trunk. |
| **Forward unwrapping** | Emails arrive nested in 2–3 "Forwarded message" layers; the real carrier is the **innermost** `From:`. |
| **Human-in-the-loop** | Wherever Dummy is unsure, it stops and asks rather than guessing. |

---

## 3. Locked business rules (do not change without the owner)

Columns we care about, in order: `prefix, destination, inter_rate, intra_rate,
indeterminate_rate, connect_fee, init_duration, increment` (+ `grace_time`, `effective_date`).

1. `prefix`, `inter_rate`, `intra_rate`, `indeterminate_rate` come **from the carrier**.
2. If the carrier gives **no** indeterminate rate:
   `indeterminate_rate = MAX(inter_rate, intra_rate)` — compare as `Decimal`, output the
   **original text** of the larger value (Excel `MAX(Inter, Intra)`).
   - ⚠️ Nuso's email says indeterminate calls are "rated as Interstate". Our MAX rule can differ
     from the carrier's own billing where intra > inter. This is the owner's explicit call.
3. `destination` is **always the literal `NANPA`** — ignore the carrier's own labels
   (`JERSEY CITY-NJ`, `CANADA - AB`).
4. `connect_fee`: carrier value if present, else `0`.
5. `init_duration` / `increment`: carrier value if present, else the trunk's configured default
   (currently **12 / 6** — most carriers moved to 12/6 on 2026-11-01), else **ask a human**.
6. `grace_time`: carrier value if present, else blank.
7. `effective_date`: **fill it if you find it in the carrier's email or CSV**, otherwise blank.
   Format is configurable (`EFFECTIVE_DATE_FORMAT`); the default is **ISO `%Y-%m-%d`**, which is what
   the portal's template and upload form use. The **file name is never the effective date** (the ANI
   file is named `10_28` but the email says effective Oct 29).
8. Read every CSV as **text** (`dtype=str`). Never convert rates to float and back — keep
   `0.0012` as `0.0012`. `Decimal` is used **only** for comparisons.
9. Never auto-upload to a trunk that has not been human-approved at least once (`approved_trunks`).

---

## 4. Facts learned from the five sample decks & four emails

Five real sample decks live in the repo root and are copied to `tests/fixtures/`:

| File | Layout | Missing vs template / notes |
|---|---|---|
| `DIAL WORLD  Prefix_6060 11_01_2026.csv` | already template-like (`prefix,destination,…`) | `effective_date` only |
| `ANI_10_28_2026_rates.csv` | `Prefix,Destination,Inter Rate,…` Title Case | `effective_date`; email names **no trunk** |
| `BLC_Platinum_Plus_Q3_2026_11_02.csv` (Nuso) | `1NPANXX,Interstate,Intrastate,Effective Date` | destination, indeterminate, connect, init, increment; arrives as a **zip**; thousands of blank `,,,` rows |
| `RATES-CONVO_2020_NPANXX_12_6-2026-11-01.csv` (Dial World's other layout) | `Name,Dialcode,Rate (inter),Intrastate,Min Dur,Inc Dur,Effective Date` | connect fee, grace time; **UTF-8 BOM** |
| `Fractel-2026-11-01-CVDeluxe-Domestic.csv` | 7 `#` comment lines; the real header is itself a `#` line: `# Code, Interstate, Intrastate, Indeterminate, Destination, EffectiveDateUTC` | connect, init, increment; date ISO datetime `2026-11-01 00:00:00`; full-size |

> Note: the current real Fractel Canada/Domestic files are full-size (up to ~206k rows) and use an
> ISO **datetime** (`2026-11-01 00:00:00`); the copies in `tests/fixtures/` are the smaller originals.

**Verified conversion facts:**
- BLC converts to (`1201200,NANPA,0.0012,0.0012,0.0012,0,12,6,, effective` / `…201…` / `…202…`) —
  identical to `rate_template_npanxx.csv` in the rate fields.
- CONVO and Dial World 6060 carry **identical rates for every shared prefix** (same deck, two layouts).
- Cleaning must handle: BOM, `#` comment lines **including a header written as a `#` line**, fully
  blank rows, `.zip`, and `.xlsx`.

**Email-parsing facts:**
- Unwrap forwards → the real carrier is the **innermost** `From:`.
- Prefer the **newest** attachment/date (Nuso quotes an older `…_10_15.csv.zip` under the new one).
- Dial World emails carry a **"Download Now" link** (valid until a stated date), plus a Tech Prefix
  (`6060` → "USA CONVO (C)", `1515` → "USA CC STD (C)") and a trunk name → match on tech prefix.
- ANI emails name **no trunk** → match sender domain → carrier, then **ask a human**.
- Nuso: unlisted NPANXX bill at a default rate (**$0.06**) → surface to the human, do not invent a row.
- Flag billing-increment changes mentioned in emails (e.g. "6/6 → 12/6 effective 2026-11-01").
- Fractel header lists short-duration penalty terms → surface when they change.

**Flagged for a human (not auto-handled):**
- ANI email says "non-domestic" but the sample deck is NANPA (+1 201…) — verify deck type.
- Nuso $0.06 default: decide whether the switch needs an explicit default row.
- `grace_time` = `0` in ANI vs blank elsewhere — confirm whether 0 and blank are the same on the switch.

---

## 5. Output template (our format)

Header, exactly, in this order:

```
prefix,destination,inter_rate,intra_rate,indeterminate_rate,connect_fee,init_duration,increment,grace_time,effective_date
```

Reference sample: `rate_template_npanxx.csv`.

---

## 6. System architecture

### 6.1 Module map (target, AGENTS.md §5)

```
dummy/
  config.py         .env + paths + conversion defaults            [BUILT]
  portal.py         Playwright client: login, carriers,            [M1 BUILT — reader]
                    carrier_trunks, download_template, create_rate_deck, upload
  parse.py          pure HTML parsing (unit-testable)              [BUILT]
  scrape_trunks.py  CLI: /carriers.php -> data/trunks.csv (+carriers.csv)  [BUILT]
  cleaner.py        messy CSV/xlsx/zip -> clean str DataFrame      [BUILT — M2]
  mapper.py         aliases + per-carrier profile -> template      [BUILT — M2]
  validator.py      template -> list[Issue] (error/warn)           [BUILT — M2]
  convert.py        CLI wiring cleaner+mapper+validator            [BUILT — M2]
  trunk_matcher.py  sender/tech-prefix/product -> trunk + conf.    [BUILT — M3]
  store.py          SQLite: runs, decisions, trunk_config,         [BUILT — M3/M9]
                    approved_trunks, pending queue, deck_history
  review.py         pending queue + matcher CLI                    [BUILT — M3/M9]
                    (list / resolve / approve-trunk / match)
  portal.py (writer) create_rate_deck / upload (dry-run first)     [BUILT — M4]
  upload.py         CLI: dry-run plan / --commit                   [BUILT — M4]
  mailbox.py        IMAP reader, unwrap forwards, attachments      [BUILT — M5]
  fetcher.py        download attachment/link (Playwright fallback) [BUILT — M5]
  email_llm.py      LLM extraction -> pydantic + rule fallback     [BUILT — M6]
  orchestrator.py   pipeline + scheduler + notifications + audit   [BUILT — M7]
  input_sources.py  email or local-directory input adapter         [BUILT — M8]
  web.py            authenticated admin over the pending queue     [BUILT — M10]
  templates/        Jinja UI: login / dashboard / pending detail
  cleanup.py        delete decks older than N months (guarded)     [BUILT — M15]
  tests/ + tests/fixtures/
```

**Hard boundaries (AGENTS.md §2):**
- Portal selectors/URLs live **only** in `portal.py`.
- HTML parsing lives **only** in `parse.py` (pure functions, unit-testable).
- The LLM understands email text and may **propose** a column mapping for an unknown layout as
  JSON — it **never rewrites CSV rows**; pandas does the conversion.
- Original downloads are never deleted/overwritten; archived under
  `data/archive/<date>/<message-id>/`.

### 6.2 Pipeline (AGENTS.md §5)

```
source (email | directory) -> email_llm -> fetcher -> trunk_matcher -> (human if unsure)
        -> cleaner -> mapper -> validator -> dry-run report
        -> (human approval if first time for the trunk)
        -> create deck -> upload -> assign to trunk -> verify -> audit log + notification
```

Mapping/cleaning/validation is deterministic and offline (M2, done). LLM use is limited to
email understanding and proposing mappings for unknown layouts; approved mappings are saved per
carrier/trunk so later runs are deterministic.

### 6.3 Per-trunk config

`carrier_id, trunk_name, tech_prefix, sender_domains, default_init_duration, default_increment,
header_aliases, approved(bool)`.

- M2 reads an optional **YAML** file via `convert.py -c trunk.yaml` (`default_init_duration`,
  `default_increment`, `header_aliases`).
- M3 moves the canonical config into **SQLite** (`store.py`).

### 6.4 Portal facts (verified from real pages)

- Nav is `/carriers.php`; each carrier box links to `/carrier_trunks.php?id=N` (anchor `class="btn btn-trunks"`).
- Carrier page: name in `.pg-head h1`; each trunk is a `.cpanel` card — name in `.cpanel-head > strong`,
  trunk id from the `edit=` link (`/carrier_trunks.php?id=41&edit=63`), an optional `prefix:` `<code>`
  badge (the tech prefix), a `.cbadge` status (`Active`/`Disabled`), and a `.cost-deck` badge holding the
  existing deck name + date.
- The `.ip-table` inside each card is the IP list — **not** the trunk list (the old table-based parser
  wrongly grabbed it, which is why `trunks.csv` contained IPs).
- Verified scale: **25 carriers, 52 trunks**. Ground truth for M3: carrier 58 "DW Telco" = Dial World
  ("DW USA CONVO TRUNK 6060" prefix `6060`, "DW USA STD CC TRUNK 1515", …); carrier 42 = ANI Network
  ("ANI Network LD"); carrier 59 = BTCL ("BTCL TNT TRUNK"); carrier 35 = Nuso ("NOS PLATINUM Trunk",
  deck "NUSO Platinum Plus 11_02_2026").
- **Template** (`download_template()` → `data/rate_template_npanxx.csv`): header identical to ours;
  the upload page maps columns by **name** ("auto column mapping"). Portal dates are ISO
  (`2026-11-03`); the template's `destination` holds demo city names (our rule stays `NANPA`).
- **Create deck** (`rate_decks.php` → POST `/actions/rate_deck_save`): `name` ("<Trunk> <date>"),
  `type=carrier`, `mode=npanxx`, `currency=USD`, and checkbox `lrn_billing` = "Use LRN for billing".
- **Upload** (`rate_deck_upload.php?id=<deck_id>`): `_csrf`, hidden `deck_id`, `csv_file`,
  `effective_date` (date input; "CSV may override per-row via `effective_date` column"), `replace_mode`.

---

## 7. Human-in-the-loop & decision points

Dummy **stops and asks** (AGENTS.md §2) when:
- the trunk is not found or the match is low-confidence;
- the column mapping is low-confidence (unknown layout);
- validation raises errors, or warnings need approval;
- a first-ever upload to a trunk (not in `approved_trunks`);
- an unexpected rate jump / large row-count drop is detected;
- ambiguous email signals (ANI names no trunk; unlisted-NPANXX default rate; deck-type mismatch).

`review.py` is the CLI prompt for these decisions (email/Slack/Telegram later). Every decision is
persisted in `store.py`.

---

## 8. Safety, secrets & data handling (hard rules)

- Portal is live billing infrastructure → **dry-run by default**; write only with `--commit`.
- Never auto-upload to a not-yet-approved trunk.
- Never print, log, or commit secrets. `.env` is git-ignored; do not hardcode credentials.
- Keep `.env` owner-only; prefer a dedicated low-privilege portal user for Dummy.
- Never delete/overwrite original downloaded files — archive them.
- Tests must never hit the live portal or the mailbox — always mock.
- Back up the audit log (original email, original file, converted file, decisions): it is the
  evidence used in a rate dispute.

---

## 9. Milestones & status

| # | Milestone | Status |
|---|---|---|
| **M1** | Trunk scraper: `portal.py`/`parse.py` → `data/trunks.csv` | **Verified against real snapshots.** Login works, card-based trunk parsing, `/carriers.php` id discovery. 25 carriers / 52 trunks re-parsed offline. |
| **M2** | Cleaner + mapper + validator + `convert.py` (offline) | **Done.** All fixtures convert; BLC verified; CONVO ≡ Dial World; real full decks (~240k rows) convert in ~1s. |
| **M3** | Trunk matcher + `store.py` (SQLite) + `review.py` CLI | **Done.** Prefix `6060` → `DW USA CONVO TRUNK 6060`; ANI/unknown/shared-prefix → human. |
| **M4** | Portal writer, dry-run first (`create_rate_deck`/`upload`, `upload.py`) | **Done & verified live** on `BTCL TNT TRUNK`: created deck 178 (`BTCL TNT TRUNK 11_02_2026`, LRN ticked, carrier/npanxx), uploaded 5 rates ("Import Complete"). Dry-run + refusal also verified. **M8** adds trunk **assignment + verify** + orphan cleanup — **verified live** (deck 180 assigned to trunk 87). |
| **M5** | Mailbox + fetcher (IMAP, forward-unwrap, attachment/zip/link, idempotent by Message-ID) | **Done.** `.eml` fixtures for all 4 sample emails; innermost-sender unwrap, attachments/zip/link, Message-ID idempotency. Live IMAP connect reserved for M7. |
| **M6** | Email understanding (LLM extraction + rule fallback, pydantic-validated) | **Done.** `EmailFacts` via rule-based extraction + OpenAI-compatible (DeepSeek default) overlay; API mocked in tests. Verified on all 4 sample emails. |
| **M7** | Orchestrator + scheduler + notifications + audit log + Dockerfile + VPS README | **Done.** Full pipeline + `--loop` scheduler, console/log notifications, JSONL audit + archive, `Dockerfile`, `DEPLOY.md`. E2E tested with mocks. |

---

## 10. Current implementation notes (for whoever picks this up)

Delivered and green (`python -m pytest -q` → 162 passed, offline):

- **`cleaner.read_clean(path) -> DataFrame[str]`** — `.csv`/`.xlsx`/`.zip`; UTF-8-sig (BOM) with
  latin-1 fallback; trims cells; drops fully blank rows; drops `#` comment lines and **detects a
  header written as a `#` line**; reads xlsx via `header=None`; pads/truncates short rows.
- **`mapper.to_template(df, trunk_config=None) -> DataFrame`** — alias resolution (headers
  normalized to lowercase/space; underscores → spaces, so `inter_rate` matches `Inter Rate`);
  `destination="NANPA"`; `indeterminate = MAX(inter,intra)` (original text) when missing;
  `connect_fee` default `"0"`; `init_duration`/`increment` default `12`/`6`; `effective_date`
  parsed to `EFFECTIVE_DATE_FORMAT`. `TEMPLATE_COLUMNS` is the canonical column order.
- **`validator.validate(df, previous=None, overrides=None) -> list[Issue]`** — header match,
  required columns, prefix digits-only (error) + NPANXX 7-digit length (warning), duplicate
  prefixes, numeric/non-empty rates, positive-int durations, `effective_date` parses; warnings vs a
  previous deck: row-count drop > 5%, and `--rate-change-pct` (default 20%) changes across
  > 1% of rows. Errors block; warnings allow.
- **`convert.py`** — `python convert.py <file> [-c trunk.yaml] [-o out.csv] [--previous prev.csv]`;
  errors → exit 1 and no output; warnings → writes and exit 0.
- **`config.py`** — added `EFFECTIVE_DATE_FORMAT` (default `%Y-%m-%d`), `DEFAULT_INIT_DURATION=12`,
  `DEFAULT_INCREMENT=6`.
- **`tests/fixtures/`** — copies of the 5 root sample decks.

**Decision recorded:** the BLC verified expectation shows a blank `effective_date`, but the BLC
file has an `Effective Date` column and §4 says "if the file has a date column, it **is** filled".
Resolution: **fill and normalize to ISO `%Y-%m-%d`** (BLC → `2026-11-02`), matching the portal's
template CSV and upload form (open question #1 → resolved). The 9 rate fields still match the doc
string exactly.

- **Performance:** mapper/validator are vectorised (no `iterrows`). A 237k-row deck converts in
  ~1s (read ~1–2s), total <5s per full deck. Verified on the real decks now in the repo root.
- **Date inputs handled:** `2026-11-01 00:00:00` (ISO datetime), `11/1/2026 0:00`, `11/2/2026`,
  `11/2/26` (2-digit year), `2026-11-01` → all normalised to ISO `%Y-%m-%d`.

**Known limits:** `.xls` (legacy) reading needs `xlrd` (not installed) — only `.xlsx` verified.
Previous-deck thresholds are best-effort defaults, tunable via `--rate-change-pct`.

### M3 — matcher / store / review (done)

- **`store.py`** — SQLite (`config.DB_PATH` = `data/dummy.db`): `carriers`, `trunks`, `carrier_senders`,
  `trunk_config`, `approved_trunks`, `messages`, `runs`, `decisions`. `load_trunks()` imports
  `data/trunks.csv`; `add_message()` is idempotent by Message-ID.
- **`trunk_matcher.py`** — `TrunkHints` (pydantic) → `match_trunks()` → `MatchResult` (trunk,
  confidence, candidates, `needs_human`). Exact **tech prefix** = 1.0; else **rapidfuzz** name/deck
  match; a matching sender carrier adds +0.1 and breaks ties. Auto-accept threshold
  `config.MATCH_THRESHOLD` (**0.85**). A sender→carrier match **alone** never auto-selects, so a
  single-trunk carrier still asks a human. Equal top candidates → `ambiguous` → human.
- **`review.py`** — `choose_trunk()` auto-accepts a confident match, else shows a numbered prompt
  (`input_fn` injectable for tests); `resolve_and_record()` writes the outcome to `decisions`.
- Verified on the real data: `6060 → DW USA CONVO TRUNK 6060` (1.0), `1515 → DW USA STD CC TRUNK 1515`;
  ANI / unknown prefix / shared prefix `1111` → `needs_human`.

### M4 — portal writer (done, dry-run first)

- **`parse.extract_decks(html)`** — deck id/name/type/mode/status from `.rd-card` (`id="rd-card-N"`).
- **`portal.create_rate_deck(name, …, lrn_billing=True) -> deck_id`** — `#addForm` →
  POST `/actions/rate_deck_save` (`type=carrier`, `currency=USD`, `mode=npanxx`, `enabled=1`,
  `lrn_billing=1`); then finds the new id by name in the reloaded list (raises if not found).
- **`portal.upload_rate_deck(deck_id, csv, effective_date=, replace=True)`** — `#uploadForm` multipart
  (`csv_file`, `effective_date`, `replace_mode=1`); snapshots before/after and classifies success.
- **`upload.py`** — dry-run by default (prints deck name `<Trunk> MM_DD_YYYY`, "Use LRN for billing",
  replace, file + row count). `--preview` screenshots the pages; `--commit` writes, gated to
  `BTCL TNT TRUNK` or an approved trunk. `--deck-id` targets an existing deck; `--no-replace` disables it.
- Deck-name date = the deck's effective date (`config.DECK_NAME_DATE_FORMAT` = `%m_%d_%Y`), else today.
- ✅ Verified live on `BTCL TNT TRUNK`: created deck **178** (`BTCL TNT TRUNK 11_02_2026`, `lrn_billing=True`,
  carrier/npanxx) and uploaded 5 rates ("Import Complete"). Success classification matched `imported`.

### M5 — mailbox + fetcher (done)

- **`mailbox.parse_email(raw)`** → `ParsedEmail(message_id, subject, sender, date, body_text,
  attachments, links)`. `sender` is the **innermost** forwarder (the real carrier): walks
  `message/rfc822` parts depth-first, else the last `From:` in the body, else the message's own `From`.
- **`mailbox.ingest(store, raw)`** — parses and records; returns `None` on a repeat Message-ID
  (idempotent). `Mailbox` is a thin `imaplib.IMAP4_SSL` client (`IMAP_*` in `.env`).
- **`fetcher`** — `save_attachment` (sanitized names), `extract_zip`, `download_url` (**requests**,
  Playwright fallback when `use_browser`), `fetch_deck_files` (attachments → unzip → else follow
  `download_links()`; returns `.csv`/`.xlsx`).
- Fixtures: `tests/fixtures/emails/{ani,nuso,dialworld,fractel,nested_forward}.eml`
  (`build_emails.py` regenerates them). Verified: carrier sender through 3 forward layers, zip
  attachment extraction, download link, `#`-header CSV attachment.

### M6 — email understanding (done)

- **`email_llm.EmailFacts`** (pydantic v2, `extra="ignore"`): `carrier_name, sender_domain, trunk_name,
  tech_prefix, product, effective_date (ISO), init_duration, increment, deck_filename, notes`;
  `to_hints()` bridges into `trunk_matcher.TrunkHints`.
- **`rule_based_facts(parsed)`** — always available (no key): prefix/trunk/product regexes, effective
  date (numeric **and** month-name), `N/M` increments (the new value after `->`), newest attachment for
  the filename.
- **`extract_facts(parsed, *, client=None, use_llm=None)`** — rules first, then an OpenAI-compatible
  **LLM overlay** (non-empty values win); any network / non-JSON / schema error falls back to rules.
  Default provider is **DeepSeek** via `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — an owner-approved
  deviation from AGENTS.md's "Anthropic API" wording (any OpenAI-compatible endpoint works).
- **`python email_llm.py <file.eml> [--llm]`** prints the facts. The LLM only reads email text — it
  never edits CSV rows.

### M7 — orchestrator (done)

- **`orchestrator.process_email(store, raw, *, portal, commit, use_llm, input_fn, notify, today)`** —
  the entire pipeline, returning a status of `duplicate / no_deck / needs_human / validation_error /
  dry_run / blocked / uploaded / upload_failed`. Unattended runs (`input_fn` hits EOF) mark
  `needs_human` instead of hanging.
- **`run_once` + `main`** — `--eml <file>` (dev), one-shot mailbox run, or `--loop --interval N`.
  `Portal` is imported only on `--commit`, so dry-run never touches Playwright.
- **Audit log** — `data/archive/<date>/<message-id>/` (raw `.eml`, original deck, converted CSV),
  `store` (runs / decisions / deck_history), and a `data/audit.jsonl` line per run.
- **`store.deck_history`** — `record_deck` / `last_deck` feed the "N changes" diff vs the previous deck.
- **Deployment** — `Dockerfile` (python:3.11-slim + Chromium, runs `--loop`) and `DEPLOY.md`
  (docker / systemd / cron, portal IP whitelist, backups).

### M8 — deck lifecycle + pluggable inputs (in progress)

- **Assignment is captured, not guessed.** The trunk edit page
  (`carrier_trunks.php?id=<carrier>&edit=<trunk>`) hides `select[name=rate_deck_id]` under the
  **Routing** tab; the writer clicks **Routing** first, selects the deck, clicks "Update Trunk"
  (POST `/actions/carrier_trunk_save`), then reads the cost-deck badge back to **verify**.
- **Date display:** run messages / prompts show `MM_DD_YYYY` (`config.DECK_NAME_DATE_FORMAT`); the CSV
  `effective_date` column and the portal date field stay **ISO** (`EFFECTIVE_DATE_FORMAT`), which is
  what the portal's template and date input use.
- **Write path** is now create → upload → **assign → verify**; on upload/assign failure the just-created
  deck is **deleted** (`delete_rate_deck`) so no orphans remain; `deck_history` records `deck_id`.
- **Pluggable inputs** — `input_sources.py`: `EmailSource` (IMAP) and `DirectorySource` (a folder of
  `.csv/.xlsx/.zip`; optional sidecar `<file>.json` for hints; `--trunk` override; idempotent by
  content hash). Orchestrator CLI: `--source inbox|directory --dir <folder> --trunk <name>`.
- **Discovery helpers** — `tools/inspect_assignments.py` (read-only network capture);
  `scrape_trunks --discover` now also saves script URLs.
- ✅ **Verified live** on `BTCL TNT TRUNK`: a directory `--commit` created **deck 180**
  (`BTCL TNT TRUNK 12_02_2026`), uploaded 2 rates, **assigned it to trunk 87**, and the trunk's
  cost-deck badge now reads `BTCL TNT TRUNK 12_02_2026`. Re-verified **after the Routing-tab fix**
  (deck **182**, screenshot `assign_after_87.png`, toast "Trunk saved.").

### M9 — human-loop backend (done)

- **Pending queue** (`store.pending`): a run that can't proceed cleanly **enqueues** an item instead of
  stopping — `trunk` (unresolved), `approval` (first-time upload), or `warnings`
  (with `--require-clean-warnings`). Enqueued only in `--commit` mode; dry-run stays side-effect-free.
- **Resolve/approve CLI:** `python review.py list | resolve <id> --trunk NAME | resolve <id> --approve |
  approve-trunk NAME`.
- **Resume:** the daemon (`--loop --commit`) calls `process_pending()` each pass; resolved items are
  committed (create → upload → assign → verify). A resolved non-dev trunk without approval flips to an
  `approval` item; approving it then commits. `deck_history` records only real uploads.
- Verified: 106 tests; offline flow enqueue → resolve → approve → commit with a `FakePortal`.

### M10 — web admin, Phase B (done)

- **`web.py`** (FastAPI + Jinja): single-admin **session login** (`ADMIN_USER`/`ADMIN_PASSWORD`,
  `SESSION_SECRET`), **CSRF** on every POST, per-request SQLite access. Routes: `/login`, `/` (pending +
  recent runs), `/pending/{id}` (detail + converted-CSV head), `POST /pending/{id}/resolve`
  (resolve/cancel), `/pending/{id}/download`, `/logout`.
- **Reads/writes only the DB** (`pending`/`decisions`) — no portal imports. The `--loop --commit`
  daemon applies resolved items.
- **Deploy:** `docker-compose.yml` (orchestrator + web + Caddy) and a `Caddyfile` for TLS;
  `DEPLOY.md` §6 covers domain/TLS and the SSH-tunnel alternative.
- Tests: 113 total; TestClient covers auth, CSRF, resolve/cancel, download.

### M11 — multi-deck emails (done)

- An email/file carrying **several decks** is processed **per deck**: `_process_files` dispatches to
  `_process_single` once per deck file. Each deck gets its own hints, idempotency key
  (`<source>#<filename>`), run, conversion, pending item, and upload.
- Per-deck hints: a `Prefix_NNNN` filename overrides the email's tech prefix (`_facts_for_file`), so a
  Dial World mail with `Prefix_6060` / `Prefix_1515` decks maps to trunks **82** / **83** independently.
- Verified: 116 tests; `--eml` multi demo → two dry-runs (`DW USA CONVO TRUNK 6060` → trunk 82,
  `DW USA STD CC TRUNK 1515` → trunk 83).

### M12 — carrier onboarding (done)

- **Auto-learn**: when a deck resolves to a trunk, its sender domain is recorded in `carrier_senders`
  (if unmapped) — later matches get that carrier hint for free.
- **Layout guard**: if the mapper can't resolve `prefix` / `inter_rate` / `intra_rate` from a known
  trunk's file, the run stops with `needs_mapping` (and, under `--commit`, enqueues a `mapping` pending
  with the file's headers) instead of silently emitting empty columns.
- **CLI**: `review.py set-domain <domain> <carrier_id>`, `review.py set-aliases <trunk> '<json>'`,
  `review.py config [--trunk]`; per-trunk `header_aliases` flow into `to_template`.
- Verified: 120 tests; auto-learn (`dialworld.com → 58`) and the unmapped-layout guard are covered.

### M13 — reliability & observability (done)

- **Retries/backoff** (`retry.py`): `download_url` retries transient `requests.RequestException`s
  (3 attempts, exponential backoff); any other exception propagates immediately, so the browser
  fallback still works.
- **Structured logging** (`logging_setup.configure()`): timestamped console output plus
  `data/dummy.log` (`LOG_LEVEL` / `LOG_FILE` in `.env`), configured at daemon start.
- **Healthcheck**: `GET /healthz` (no auth) returns `{ok, pending_open}` for a reverse proxy / systemd;
  `503` on DB failure.
- Verified: 124 tests.

### M14 — notifications (email only) (done)

- **`notify.Notifier`** sends to **console/log + email (SMTP)**. Email is configured via
  `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_TO` in `.env`; leave
  unset to disable it. A channel failure is logged and never breaks the pipeline.
- The orchestrator routes every `notify(...)` (run summaries, `NEEDS HUMAN`, errors) through the
  notifier.
- Verified: 126 tests (email dispatch + safe when unconfigured).

### M15 — cleanup + reuse (done)

- **`cleanup.py`** deletes rate decks **older than N months** (default **4**), aged by the date in the
  deck name (`MM_DD_YYYY` / `MM/DD/YYYY` / …). Dry-run by default; `--commit` deletes. Safety: it **skips
  decks assigned to a trunk** (`?assignments=<id>`), skips undated decks, and never acts without
  `--commit`. `python cleanup.py --older-than-months 4 [--commit] [--headed]`.
- **`--reuse-deck`** (`upload.py`): upload into the trunk's **existing** deck (`existing_deck_id`)
  instead of creating a new one; if the trunk has no deck it errors and writes nothing.
- `portal.py` gained `list_decks`, `deck_is_assigned`, `existing_deck_id`.
- Verified: 131 tests (date parsing / cutoff, assigned + undated skips, reuse + missing-deck).

### M16 — orchestrator reuse (done)

- **`--reuse-deck`** on the orchestrator: each run uploads into the trunk's **existing** deck instead of
  creating a new one — **no create, no re-assign** (it is already wired). If a trunk has no deck the run
  reports `upload_failed` and leaves it alone. Orphan cleanup only ever deletes decks **Dummy created**,
  so a reused deck is never removed.
  `python orchestrator.py --source directory --dir inbox --commit --reuse-deck`.
- Verified: 133 tests (reuse uploads without create/assign; missing deck fails safely).

### M17 — orphan-deck sweeper (done)

- **`cleanup.py --orphans`** sweeps decks Dummy created but never assigned: a deck is an orphan when its
  name is ``<managed trunk> <date>``, it is **not assigned** to any trunk, and it is older than
  `--min-age-days` (default **1**, so we never race a run that just created one). Dry-run by default;
  `--commit` deletes. `python cleanup.py --orphans [--min-age-days 1] [--commit]`.
- Verified: 135 tests (orphan selection, trunk-name prefix incl. numeric names, assigned/too-new skips).

### M18 — LLM column-mapping proposal (done)

- For an **unknown layout**, `email_llm.propose_mapping(headers, sample_rows)` asks the LLM to map the
  real headers onto the template and returns a ready-to-use ``header_aliases`` suggestion, **validated
  against the actual headers** (the LLM can never invent a column). The orchestrator stores it on the
  `mapping` pending item (`suggested_aliases: {...}` plus whether it covers all required columns).
- `review.py apply-mapping <id> [--aliases JSON]` saves the mapping to `trunk_config.header_aliases` and
  marks the item `applied`; the next source pass then converts deterministically.
- **No key / any LLM error → no suggestion** (unchanged behaviour: the item is still queued for a human).
  The LLM never rewrites rows; pandas converts.
- `process_pending` never uploads a `mapping` item (it only marks it `applied`) - it must be re-converted
  with the new aliases first.
- Verified: 141 tests.

### M19 — mailbox check CLI (done)

- **`python mailbox.py --check`** connects with the `IMAP_*` settings and reports message / unseen counts
  (`--folders` lists labels), with `--host/--port/--user/--password/--folder` overrides — **no processing,
  no writes**. Unconfigured, it prints the clear "Set IMAP_HOST …" message and exits non-zero.
- **`mark_seen`**: `Mailbox(..., mark_seen=False)` (env `IMAP_MARK_SEEN="false"`) fetches with `BODY.PEEK`
  so messages stay unread; the default (mark read) is unchanged.
- Verified: 145 tests.

### M20 — mail-client wiring (done)

- `config` accepts `DEEPSEEK_LLM` / `DEEPSEEK_API_KEY` as aliases for `LLM_API_KEY` (the `.env` used a
  name the code ignored, so the LLM was silently off).
- `notify` sends over **implicit SSL on port 465** (STARTTLS otherwise), matching cPanel's published SMTP.
- `mailbox.py --check` now prints a one-line failure instead of a traceback on a bad host / credentials.
- `tests/conftest.py` blanks `LLM_API_KEY` / `IMAP_HOST` / `SMTP_HOST` so the suite stays **offline** even
  with a real key / mailbox in `.env`.
- Configured: mailbox `instacall.us:993`, user `siam@instacall.us` (paste the password to finish).
- Verified: 147 tests.

### M21 — long Message-ID path fix (done)

- A Message-ID such as a Teams notification (`TeamsMissedActivityEmailTFL-…@odspnotify`) exceeded the
  Windows path limit and crashed the entire run (`OSError [WinError 123]`) when used as the archive
  folder name.
- One length-bounded helper, **`input_sources.safe_name()`** (≤ 80 chars, uniqueness via a trailing
  sha1), is now used by **both** the email/directory sources **and** `orchestrator.archive_dir()` — the
  duplicated, uncapped sanitizer was the root cause.
- `tests/conftest.py` also pins `IMAP_MARK_SEEN=True` so the suite no longer depends on the dev `.env`.
- Verified: 150 tests.

### M22 — inbox ergonomics (done)

- **`orchestrator.py --limit N`** processes at most N inputs (newest first for the inbox), so a large
  unseen backlog can be tested cheaply.
- `EmailSource.fetch` now **logs progress** ("email source: N message(s)" + one line per message), so a
  long fetch no longer looks hung.
- **`no_deck` no longer raises "NEEDS HUMAN"**: a mail with no deck is normal and is only logged; the
  human queue is reserved for real ambiguity (trunk/approval/mapping/warnings).
- Verified: 152 tests.

### M23 — trunk aliases + better matching (done)

- **Trunk name aliases** (`trunk_config.name_aliases`, JSON): a human declares variants, e.g.
  `review.py add-alias "BTCL TNT TRUNK" "BTCL TNT Domestic"`. An alias is an **exact (case-insensitive)
  match → confidence 1.0**, so a known naming variant resolves deterministically (declared, not guessed).
- **All name candidates are matched** — `trunk_name`, `product`, and the deck filename
  (`EmailFacts.to_hints()` now passes `deck_name`). Previously only the first non-empty one was tried, so
  the LLM's `product="TNT Domestic"` hid the filename `"BTCL TNT Domestic"`.
- **Effective-date mismatch warning**: if the email's date differs from the file's date column, the run
  carries a warning (surfaced through the approval gate).
- Bug fixes: `to_template` no longer writes the literal `"None"` when a config row has NULL defaults;
  `store.all_trunks()` now exposes `name_aliases`.
- Verified: 158 tests; live dry-run of the forwarded BTCL deck → `BTCL TNT TRUNK 11_15_2026: 205,862 rows`.

---

## Repository layout

```
*.py                  flat modules (kept at the repo root)
tests/                pytest suite + small offline fixtures
docs/                 PROMPTS.md, final_review.html
templates/            Jinja templates (web.py admin)
tools/                read-only helpers (network capture)
samples/              large real decks (git-ignored)
archive/              retired v1 trunk-scraper (git-ignored)
inbox/                drop folder for the engine (git-ignored)
data/                 runtime DB / archives / logs (git-ignored)
```

Boundaries: portal URL only in `config.py`; Playwright selectors only in `portal.py`; HTML parsing only in
`parse.py`.

## Engine (folder-first, Windows-local) — M24 (built)

A narrower UX over the **same** engine: no email/IMAP, no daemon. Implemented as `engine.py` (CLI) +
`engine_web.py` (localhost dashboard) with a `deck_jobs` table.

```
sync -> scan -> map (local web dashboard) -> convert -> dry-run -> approve -> push
```

- `engine.py` — CLI: `sync` (trunks → SQLite), `scan` (`inbox/` → jobs), `run` (convert → dry-run),
  `push` (**create → upload → assign → verify**, orphan cleanup on failure).
- `engine_web.py` — local dashboard on `127.0.0.1` (**no login**; CSRF kept) for the human file→trunk
  mapping and approval; action failures show as an on-page banner.
- `inbox/` — the drop folder; jobs live in the `deck_jobs` table.
- `launcher.py` + `start.bat` — double-click launcher: a small terminal menu, then starts the dashboard and
  opens the browser; day-to-day work stays in the browser.
- Reuses `scrape_trunks`, `input_sources.DirectorySource`, `trunk_matcher`, `cleaner` / `mapper` /
  `validator`, `upload.execute`, `store`, `notify`.

**Deployment output:** local dev = CLI; server/VPS = `orchestrator.py --loop` + `web.py` behind Caddy (TLS)
+ email notifications — no separate "output type" is needed.

---

## 11. Deployment: local first, VPS once working

**Build/test locally** — portal work needs trial and error (tuning selectors, `--headed`,
re-running against `BTCL TNT TRUNK`), and dry-run/first-approval need a human watching.

**Deploy to a VPS once it works** (2 vCPU / 4 GB is fine; pandas handles 200k rows easily) —
Dummy must poll the inbox continuously; a PC that sleeps/reboots misses decks. One stable place
for logs, audit trail, SQLite state; run as a systemd service or Docker container.

**Decide before VPS (AGENTS.md §10 #5):**
1. **Portal IP restriction** — if the switch restrict by IP, whitelist the VPS static IP.
2. **Secrets** — `.env` out of git, owner-only perms, dedicated low-privilege portal user.
3. **Placement** — a company server near the switch may beat a random VPS.
4. **Backups** — back up the audit log.

Runs the same in both places: config from `.env`, relative paths. The `Dockerfile` and the
step-by-step `DEPLOY.md` (docker / systemd / cron) are in the repo; moving to the VPS is
copy-and-start.

---

## 12. Open questions (do not guess — ask the owner)

From AGENTS.md §10 plus what surfaced during the sample review:

1. ~~`effective_date` format~~ **RESOLVED:** ISO `%Y-%m-%d` (template + upload form).
2. `grace_time`: **blank vs `0`** — same on the switch? *(owner: default blank; ANI's explicit `0` kept)*
3. Default **12/6** when the file lacks it — *(owner: keep 12/6 as the default)*.
4. Is there a **confirmation step** after the upload POST on `rate_deck_upload.php`? (No CAPTCHA/2FA.)
5. ~~Portal IP restriction~~ **NO** — reached from any IP (VPS needs no whitelist).
6. ~~A real full deck~~ **RECEIVED** — real ~240k-row decks are in the repo root.
7. ~~Actual trunk names~~ **OBTAINED** — `data/trunks.csv` (25 carriers / 52 trunks); ANI = "ANI Network LD".

Fuller list of remaining items: `STATUS.md`.

---

## 13. How to work on this project

1. Read `AGENTS.md` fully, then this file.
2. Work **one milestone at a time**; do not start later milestones unless asked.
3. Restate acceptance criteria → write tests first → implement → `pytest` until green → summarize
   what changed and what is still uncertain.
4. If a requirement conflicts with `AGENTS.md`, **ask** instead of choosing.
5. Do not refactor unrelated modules.
6. Python 3.11+, type hints, small functions, `pandas` (`dtype=str`), `pydantic` v2, `pytest`,
   `logging` (never log secrets). Keep dependencies minimal and in `requirements.txt`.
```
