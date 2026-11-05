# Dummy — Status Report

_2026-11-04 · M1–M26 done · `pytest` → **172 passed** (offline)_

## TL;DR

Dummy is feature-complete against `AGENTS.md` M1–M7. The pipeline runs end to end in **dry-run** and
uploads only behind the M4 gate. **M1 and M4 are verified against the live switch portal**; **M2 is
verified on the real ~240k-row decks**; M3/M5/M6 are verified on real trunk data and `.eml` fixtures;
M7 is verified with mocks plus an offline dry-run. What remains is **operational** (live IMAP creds,
first-time trunk approvals, VPS deploy) — then optional hardening.

---

## 1. What's done (by milestone)

| # | Milestone | Status | Evidence |
|---|---|---|---|
| M1 | Trunk scraper (`portal.py`, `parse.py`, `scrape_trunks.py`) | ✅ live | Login + `/carriers.php` discovery; **25 carriers / 52 trunks** scraped; card parser fixed (it used to grab IPs) |
| M2 | Cleaner + mapper + validator + `convert.py` | ✅ | All real decks convert; BLC rows match the template; CONVO ≡ Dial World; full decks convert in ~1s |
| M3 | `store.py` (SQLite) + `trunk_matcher.py` + `review.py` | ✅ | `6060 → DW USA CONVO TRUNK 6060`; ANI / unknown / shared-prefix → human |
| M4 | Portal writer (`portal.py` + `upload.py`) | ✅ live | Created **deck 178** `BTCL TNT TRUNK 11_02_2026` (LRN ticked, carrier/npanxx), uploaded 5 rates ("Import Complete") |
| M5 | `mailbox.py` + `fetcher.py` | ✅ | `.eml` fixtures for all 4 sample emails; innermost-sender unwrap through 3 forwards; zip/link extraction; Message-ID idempotency |
| M6 | `email_llm.py` | ✅ | `EmailFacts` via rules + OpenAI-compatible (DeepSeek default) overlay; API mocked in tests |
| M7 | `orchestrator.py` + `Dockerfile` + `DEPLOY.md` | ✅ | Full pipeline + `--loop`; dry-run verified offline; JSONL audit + archive |

**Modules (all built):** `config, portal, parse, scrape_trunks, cleaner, mapper, validator, convert,
store, trunk_matcher, review, upload, mailbox, fetcher, email_llm, orchestrator`.

**Tests:** 92, all offline/mocked (parser, converter, store, matcher, review, upload, mailbox, fetcher,
email_llm, orchestrator e2e).

---

## 2. Verified vs not

**Verified live (switch portal):**
- Login/carrier/trunk scraping; deck **create** (`name`, `type=carrier`, `mode=npanxx`, `lrn_billing`);
  deck **upload** (`csv_file`, `effective_date`, `replace_mode`) on `BTCL TNT TRUNK`.

**Verified offline on real data:**
- Conversion of every real deck (BLC 237k, Fractel 206k/29k, CONVO 228k, SD 242k, Dial World 1515 242k).
- Trunk matching against the real `data/trunks.csv`.
- Email parsing on the four sample-email fixtures.

**Not yet exercised live:**
- IMAP connect + the full `--loop` against a real inbox.
- `--commit` on a real (non-dev) carrier trunk (needs first-time approval).
- The live DeepSeek extraction (rule-based path is verified).
- `docker build` / VPS run.

---

## 3. What's left (operational, not code)

1. **`.env`**: real `IMAP_HOST/PORT/USER/PASSWORD` (+ optional `LLM_API_KEY`).
2. **First pass**: `python orchestrator.py --loop` in dry-run; it will report `NEEDS HUMAN` for trunks
   it can't confidently match.
3. **Approve trunks** once each (so later runs auto-proceed), then run with `--commit`.
4. **VPS**: `docker build -t dummy .` + `docker run …` (or the systemd unit in `DEPLOY.md`); back up
   `data/archive`, `data/dummy.db`, `data/audit.jsonl`. Portal is **not IP-restricted** (confirmed).

---

## 4. Known gaps, limits & risks

- **Portal writer** selectors and the upload **success classification** come from saved snapshots;
  validated once live on BTCL. A real carrier deck is the next proof.
- **IMAP** client is thin and untested against a server.
- **Validator thresholds** (row-drop 5%, rate-change 20% / 1%) are best-effort defaults.
- **Warnings don't block** the orchestrator (only errors do); first-time uploads are gated by
  approval, but warnings aren't separately acknowledged.
- **Deck proliferation**: every run creates a **new** portal deck for the trunk.
- `grace_time` **0 vs blank** not confirmed on the switch; `.xls` needs `xlrd`.
- The 5 root sample files include some **small/old** copies; the full decks are also present in root.

---

## 5. Open questions

**Resolved:** effective_date format (ISO `%Y-%m-%d`); actual trunk names; IP restriction (none); a
full deck (received); no CAPTCHA/2FA on upload.

**Remaining:**
1. `grace_time` blank vs `0` — same on the switch?
2. Where the default **12/6** should come from per carrier when the file lacks it.
3. Is there a **confirmation step** after the upload POST on `rate_deck_upload.php`?
4. Nuso **$0.06 default-rate** for unlisted NPANXX — needs an explicit default row?

---

## 6. Candidate improvements (to weigh in the architecture review)

Not decided — ideas only:
- **Approved-trunk CLI** (`review.py approve/list`) + gating **warnings** behind acknowledgement.
- **Reuse the trunk's existing deck** instead of creating a new one each run.
- **LLM column-mapping proposal** for unknown layouts (AGENTS.md §5), persisted per carrier/trunk.
- **Effective-date precedence**: when email and CSV disagree, prefer one and **flag the mismatch**.
- **Nuso default-rate** and **ANI deck-type** checks as explicit validator rules.
- **Notifications** beyond console/log (email/Slack/Telegram) — deferred in M7.
- **Retry/backoff** around IMAP and portal calls; broaden portal-writer tests.
- **Secret hygiene**: dedicated low-privilege portal user; rotate the login after development.

---

## 7. M8 status — deck lifecycle + pluggable inputs (in progress)

**Done (code + mocked tests):**
- **Trunk assignment** — captured from the portal: the trunk edit form
  (`carrier_trunks.php?id=<carrier>&edit=<trunk>`) has `select[name=rate_deck_id]` and POSTs to
  `/actions/carrier_trunk_save`. `portal.assign_deck_to_trunk()` sets it; `portal.trunk_deck()` reads
  the cost-deck badge back to verify.
- **Write path** = create → upload → **assign → verify**, with **orphan cleanup** (`delete_rate_deck`)
  on upload/assign failure; `deck_history.deck_id` recorded.
- **Pluggable inputs** — `input_sources.py` (`EmailSource`, `DirectorySource`); orchestrator
  `--source inbox|directory --dir <folder> --trunk <name>`; sidecar hints; content-hash idempotency.
- **Discovery** — `tools/inspect_assignments.py`; `scrape_trunks --discover` saves script URLs.
- Verified offline: directory dry-run → `DRY RUN: BTCL TNT TRUNK 2026-11-02: 5 rows, 5 changes`.

**Verified live:** a directory `--commit` on `BTCL TNT TRUNK` created deck **180**
(`BTCL TNT TRUNK 12_02_2026`), uploaded it, assigned it to trunk 87, and the trunk's cost-deck badge
updated (toast "Trunk saved."). Re-verified via the **Routing tab** (deck **182**).

**Still to do:**
1. `--reuse-deck` escape hatch (target the trunk's existing deck) — planned, not implemented.

---

## 8. M9 status — human-loop backend (done)

- **Pending queue** (`store.pending`): `trunk` / `approval` / `warnings` items, enqueued on `--commit`
  when a run can't proceed cleanly (dry-run never enqueues).
- **`review.py`**: `list`, `resolve <id> --trunk|--approve`, `approve-trunk`, `match`.
- **Daemon resume**: `process_pending()` runs each `--loop --commit` pass; resolved items commit
  (create → upload → assign → verify); a resolved non-dev trunk flips to `approval` until approved.
- `--require-clean-warnings` gates uploads on validator warnings.
- Verified: 106 tests; offline enqueue → resolve → approve → commit with a `FakePortal`.

**Next (Phase B):** the authenticated **web admin** over the same `pending` / `decisions` tables.

---

## 9. M10 status — web admin (done)

- FastAPI + Jinja dashboard over the same SQLite: **session login**, **CSRF**, pending list, detail with
  converted-CSV head, **resolve/cancel**, download. **No portal access** — the `--loop --commit` daemon
  applies resolved items.
- Deploy: `docker-compose.yml` (orchestrator + web + Caddy TLS) + `Caddyfile`; `DEPLOY.md` §6.
- Tests: 113 total (auth, CSRF, resolve/cancel, download via TestClient).

---

## 10. M11 status — multi-deck emails (done)

- One input carrying several decks is processed **per deck** (`_process_single` per file), each with its
  own hints, idempotency key (`<source>#<filename>`), run, conversion, pending item, and upload.
- A `Prefix_NNNN` filename overrides the email's tech prefix, so Dial World `Prefix_6060` / `Prefix_1515`
  decks map to trunks **82** / **83** independently.
- Verified: 116 tests; multi-deck `--eml` demo → two dry-runs (DW 6060 → 82, DW 1515 → 83).

---

## 11. M12 status — carrier onboarding (done)

- **Auto-learn**: a resolved trunk records its sender domain in `carrier_senders` (if unmapped), so
  later matches get the carrier hint for free.
- **Layout guard**: if `prefix` / `inter_rate` / `intra_rate` can't be resolved, the run stops with
  `needs_mapping`; under `--commit` a `mapping` pending is enqueued with the file's headers.
- **CLI**: `review.py set-domain`, `set-aliases`, `config`; per-trunk `header_aliases` flow through.
- Verified: 120 tests.

---

## 12. M13 status — reliability & observability (done)

- **Retries/backoff** (`retry.py`): `download_url` retries transient `requests` errors (3× backoff);
  other failures still fall through to the browser.
- **Logging** (`logging_setup.configure()`): timestamped console + `data/dummy.log`
  (`LOG_LEVEL` / `LOG_FILE`).
- **Healthcheck**: `GET /healthz` → `{ok, pending_open}` (`503` on DB failure).
- Verified: 124 tests.

---

## 13. M14 status — notifications (email only) (done)

- `notify.Notifier` sends to **console/log + email (SMTP)** (`SMTP_*` in `.env`; unset disables it).
  Channel failures are logged, never fatal. The orchestrator routes every `notify(...)` through it.
- Verified: 126 tests (email dispatch + safe when unconfigured).

---

## 14. M15 status — cleanup + reuse (done)

- **`cleanup.py`** deletes decks **older than 4 months** (age from the date in the deck name); dry-run
  default, `--commit` to delete; **skips decks assigned to a trunk** and undated decks.
- **`--reuse-deck`** (`upload.py`): upload into the trunk's existing deck instead of creating one.
- Portal gained `list_decks`, `deck_is_assigned`, `existing_deck_id`.
- Verified: 131 tests.

---

## 15. M16 status — orchestrator reuse (done)

- **`--reuse-deck`** on the orchestrator: each run uploads into the trunk's existing deck (no create, no
  re-assign). A trunk with no deck fails safely; orphan cleanup never deletes a reused deck.
- Verified: 133 tests.

---

## 16. M17 status — orphan-deck sweeper (done)

- **`cleanup.py --orphans`** deletes Dummy-created decks that were **never assigned**: name is
  ``<managed trunk> <date>``, unassigned, and older than `--min-age-days` (default 1). Dry-run default.
- Verified: 135 tests.

---

## 17. M18 status — LLM column-mapping proposal (done)

- Unknown layout → `email_llm.propose_mapping` returns a validated `header_aliases` suggestion, stored on
  the `mapping` pending item; `review.py apply-mapping <id>` saves it and marks the item applied.
- No key / LLM error → no suggestion (still queued for a human). `process_pending` never uploads a
  mapping item.
- Verified: 141 tests.

**Remaining roadmap:** secrets hygiene; a real live IMAP end-to-end run.

---

## 18. Final consolidated review (M1–M19)

**Verification evidence (all offline — no portal, no IMAP):**
- `python -m pytest -q` → **145 passed**.
- `python -m compileall -q .` → clean (no syntax errors anywhere).
- `python convert.py "RATES-CONVO_2020_NPANXX_12_6-2026-11-01.csv" -o out.csv` → **228,035 rows** converted;
  rates kept as exact strings; one `prefix_length` warning (non-7-digit prefixes in the source).
- `python orchestrator.py --source directory --dir <folder> --trunk "BTCL TNT TRUNK"` → 2 items
  **dry-run**, nothing written to the portal.

**Boundary audit (AGENTS.md §2):**
- Portal URL appears only in `config.py` (`PORTAL_BASE_URL`); all Playwright selectors live only in
  `portal.py`.
- All HTML parsing lives only in `parse.py` (`BeautifulSoup` appears nowhere else).
- No hardcoded secrets; `.env` and `data/` are git-ignored; credentials are read from the environment
  only.
- Every portal write is dry-run by default (`--commit` required); dev writes are constrained to
  "BTCL TNT TRUNK".
- Originals are archived under `data/archive/<date>/<id>/` and never overwritten.

**Solid:** deterministic conversion (pandas, exact-string rates); strict column-mapping validation (the
LLM can only propose real headers); dry-run + audit trail + idempotency; async human-in-the-loop queue;
141 offline tests.

**Risks / resolve before production:**
1. **Live IMAP is unverified** — the only path exercised only with `.eml` fixtures.
2. Portal upload **+ assignment** verified only on **BTCL TNT TRUNK**.
3. `SESSION_SECRET` has an insecure default if unset; set it and enable `https_only` behind TLS.
4. **Is the portal IP-restricted?** (decides whether a VPS can reach it).
5. Nuso unlisted-NPANXX default rate is surfaced to a human, never invented (by design).
6. AGENTS §10 open questions still open: `effective_date` format, `grace_time` blank vs `0`, where the
   12/6 default comes from per carrier, portal upload confirm step.

---

## 19. M19 status — mailbox check CLI (done)

- `python mailbox.py --check [--folders] [--host/--port/--user/--password/--folder]` validates the IMAP
  credentials and reports message/unseen counts without processing; unconfigured it fails cleanly.
- `IMAP_MARK_SEEN="false"` leaves messages unread (`BODY.PEEK`); default (mark read) unchanged.
- Verified: 145 tests.

---

## 20. M20 status — mail-client wiring (done)

- `.env` mailbox verified live: **`instacall.us:993`**, user `siam@instacall.us` → `INBOX` 242 msgs /
  171 unseen. `IMAP_MARK_SEEN="false"` set so reads leave mail unread (personal-mailbox testing).
- `.env` LLM set explicitly (`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`); a live DeepSeek call returned
  valid facts. `config` also accepts `DEEPSEEK_LLM` / `DEEPSEEK_API_KEY` aliases.
- Notifications support **implicit SSL on port 465** (cPanel's published SMTP); STARTTLS otherwise.
- `tests/conftest.py` forces the suite offline regardless of `.env`.
- Verified: 147 tests.

---

## 21. M21 status — long Message-ID path fix (done)

- Live inbox dry-run surfaced a crash: a Teams-notification Message-ID was long enough to break the
  Windows path when used as the archive folder name (`WinError 123`), aborting the whole run.
- Fixed with one length-bounded `input_sources.safe_name()` (≤ 80 chars + sha1 suffix), now used by the
  email/directory sources and `orchestrator.archive_dir()`.
- `tests/conftest.py` pins `IMAP_MARK_SEEN=True` (suite no longer depends on `.env`).
- First live inbox dry-run result (mailbox untouched — `BODY.PEEK`): 171 messages → 130 `no_deck`,
  39 `validation_error` (internal `instacall_provisioned_numbers_*.csv`, not rate decks), 3 `needs_human`
  (same internal CSVs, no matching trunk), 0 real carrier decks present. Nothing uploaded.
- Verified: 150 tests.

---

## 22. M22 status — inbox ergonomics (done)

- Live inbox tests are slow/noisy on a big personal mailbox (172 unseen → every run reprocesses them).
- Added **`--limit N`** (newest first) + **progress logging** in `EmailSource.fetch`, and made
  **`no_deck` quiet** (no more "NEEDS HUMAN" per non-deck mail).
- First real forwarded deck (`BTCL TNT Domestic.csv`) was found and read, but the forward came from
  **Gmail** with no sender/prefix/`Trunk:` signal → correctly held as `needs_human` ("trunk not resolved").
- Verified: 152 tests.

---

## 23. M23 status — trunk aliases + better matching (done)

- **Trunk name aliases**: `review.py add-alias "BTCL TNT TRUNK" "BTCL TNT Domestic"` → exact match, 1.0.
- **Matcher now tries all name candidates** (trunk name, product, deck filename) instead of just the
  first non-empty one (the LLM's `product` was masking the filename).
- **Effective-date mismatch warning** when the email date ≠ the file's date column.
- Bug fixes: `to_template` no longer emits the string `"None"` for NULL config defaults; `all_trunks()`
  exposes `name_aliases`.
- **Live proof**: `python orchestrator.py --source inbox --limit 1` →
  `DRY RUN: BTCL TNT TRUNK 11_15_2026: 205862 rows, 205862 changes`.
- Verified: 158 tests. Also added a proper `.gitignore` (`.env` kept out, `.env.example` kept in,
  `data/`, caches, IDE/OS files).

---

## 24. Repo reorg + core-engine plan (done / planned)

Decisions (owner): core engine = **Windows-local, folder-first**, reusing modules; human mapping via a
**local web dashboard** (127.0.0.1); **stay flat** (no `dummy/` package); VPS output = **web + email**.

Repo organisation: sample decks → `samples/`, retired v1 scraper → `archive/`, `PROMPTS.md` → `docs/`,
new `inbox/` drop folder, stray `11` removed — `samples/`, `archive/`, `inbox/` git-ignored. `.gitignore`
extended.

Answers logged: `grace_time` blank; default **12/6**; no CAPTCHA; portal IP restriction still unknown.
`AGENTS.md` §10 updated.

Engine **built** (branch `engine`): `engine.py` (`sync/scan/list/map/run/push`), `engine_web.py` (localhost
dashboard), `deck_jobs` table. Smoke test: `scan → map → run` gave `dry_run` (237,079 rows) → `push`
dry-run wrote nothing. Full plan in `.commandcode/plans/core-engine.md`.
- Fixed a dashboard bug: Playwright's **sync** API ran inside FastAPI's event loop (commit Push 500'd);
  push/run now run in a **worker thread**, and a browser is opened only for an **allowed** trunk.
- Dashboard: **no login** (localhost-only tool); CSRF kept; action failures show as an on-page banner
  instead of a 500.
- **Fixed:** the engine push now does **create → upload → assign → verify** — it previously only created +
  uploaded, so the trunk was never actually updated — and it deletes the orphan deck on any failure.
- Verified: **165 tests**. First real write on BTCL TNT TRUNK made **deck 187** (uploaded but *not*
  assigned, due to the old code) — clear it with `python cleanup.py --orphans --min-age-days 0`.

---

## 25. Launcher (done)

- **`start.bat`** (double-click) runs `launcher.py`: a terminal menu whose option 1 starts the dashboard on
  **127.0.0.1:8090** and opens the browser; everything else happens in the dashboard (no arguments needed).
- Menu also: refresh trunks / scan inbox / convert mapped / status. Direct form for pros:
  `python launcher.py <action>`.
- Default port 8090 (`set DUMMY_PORT=...` to change); handles missing deps and stdin EOF gracefully.
- Verified: **169 tests**.

---

## 26. Dashboard UI (done)

- Redesigned `templates/engine.html`: app shell (brand + "local" pill), headline with primary actions, six
  icon stat cards, a card-wrapped table with status chips, effective-date tags, clickable suggestion chips
  (2 + "+N more"), a full-width trunk select, and **per-row actions** (Convert for mapped, Dry-run / Push
  for ready, "done" for uploaded). Added `POST /run/{job_id}` for the per-row Convert button.
- Added a **favicon** (`/favicon.svg` + `/favicon.ico`) and a **live filter bar** (search + status dropdown
  with an "N of M" count).
- Added **flash messages** (success / info / warning / error): friendly, specific copy for every action,
  colour-coded icons, dismiss button, and auto-dismiss for success/info.
- Verified: **172 tests**.
