# Dummy — carrier rate-deck automation (project spec for AI coding tools)

You are helping build **Dummy**, a Python agent for a VoIP carrier. Read this whole file before every task.
Work on **one milestone at a time** (see "Milestones"). Do not start later milestones unless asked.

## 1. What Dummy automates
Carriers email us rate decks (CSV, or a zip, or a link to a CSV). A human currently does "update the rate deck" on our SBC
switch portal (https://switchportal.instacall.digital, credentials in `.env`: PORTAL_USERNAME / PORTAL_PASSWORD). Dummy must:

1. Read rate emails from its own inbox (they arrive wrapped in 2-3 human "Forwarded message" layers).
2. Get the deck (attachment / zip / download link).
3. Work out **which trunk** the deck is for (read trunk names from the portal: `/carrier_trunks?id=<carrier_id>`). If unsure -> ask a human.
4. Convert the carrier file into **our template** (below).
5. Validate it.
6. Create a rate deck in the portal at `/rate_decks` named `<Trunk name> <date>`, with **"Use LRN for billing" ticked**, and upload the converted file.
7. Log everything (audit trail).

Test trunk on the switch: **BTCL TNT TRUNK**. Never test uploads on any other trunk.

## 2. Safety rules (hard)
- The portal is **live billing infrastructure**. Every write action (create deck, upload) defaults to **dry-run**; it only happens with an explicit `--commit` flag.
- Never auto-upload for a trunk that has not been human-approved at least once (`approved_trunks` table).
- If anything is ambiguous (trunk not found, low-confidence column mapping, validation failure, unexpected rate jump) -> **stop and ask a human**. Never guess.
- Never print, log or commit secrets. `.env` is git-ignored. Do not hardcode credentials.
- All portal-specific selectors/URLs live **only** in `portal.py`. All HTML parsing lives in `parse.py` (pure functions, unit-testable).
- Do not delete or overwrite original downloaded files; store them under `data/archive/<date>/<message-id>/`.

## 3. Our template (output format)
Header, exactly, in this order:
`prefix,destination,inter_rate,intra_rate,indeterminate_rate,connect_fee,init_duration,increment,grace_time,effective_date`

Rules (decided by the business, do not change):
- `prefix`, `inter_rate`, `intra_rate`, `indeterminate_rate` come from the carrier.
- If the carrier gives **no** indeterminate rate: `indeterminate_rate = MAX(inter_rate, intra_rate)` (compare as `Decimal`; output the original text of the larger value).
- `destination` is **always the literal `NANPA`** (ignore the carrier's destination names).
- `connect_fee`: carrier value if present else `0`.
- `init_duration` / `increment`: carrier value if present, else the trunk's configured default (currently 12 / 6 — most carriers moved to 12/6 on 2026-11-01), else ask a human.
- `grace_time`: carrier value if present, else blank.
- `effective_date`: fill **only if** found in the carrier's file or email; otherwise blank. Format is configurable (`EFFECTIVE_DATE_FORMAT`, default `%m/%d/%Y`) — OPEN QUESTION, confirm with the owner what the portal expects.
- The **file name is NOT the effective date** (ANI file is named 10_28 but is effective Oct 29).
- Read every CSV as **text** (`dtype=str`). Never convert rates to float and write them back (keep `0.0012` as `0.0012`). Use `Decimal` only for comparisons.

## 4. Carrier file quirks (see `tests/fixtures/`, these are real samples)
| File | Layout | Notes |
|---|---|---|
| `ANI_*` | `Prefix,Destination,Inter Rate,Intra Rate,Indeterminate Rate,Connect Fee,Init Duration,Increment,Grace Time` | has everything except effective date (email says effective Oct 29) |
| `DIAL_WORLD__Prefix_6060_*` | already template-like (snake_case, no effective_date) | email gives Trunk "USA CONVO (C)", Tech Prefix 6060, effective 2026-11-01 |
| `RATES-CONVO_*` | `Name,Dialcode,Rate (inter),Intrastate,Indeterminate,Min Dur,Inc Dur,Effective Date` | UTF-8 **BOM**; same rates as Dial World 6060 in another layout |
| `BLC_Platinum_Plus_*` | `1NPANXX,Interstate,Intrastate,Effective Date` | no indeterminate/connect/init/increment; thousands of blank `,,,` rows; comes as a zip |
| `Fractel-*` | 7 lines starting with `#`; the real header is itself a `#` line: `# Code, Interstate, Intrastate, Indeterminate, Destination, EffectiveDateUTC` | date like `11/1/2026 0:00`; no connect/init/increment |

Cleaning requirements: strip BOM, trim whitespace in headers/cells, drop fully blank rows (`,,,`), drop `#` comment lines but **detect a header written as a `#` line**, handle `.zip` (extract the CSV), handle `.xlsx` too.

**Verified expectation (must be a test):** converting `BLC_Platinum_Plus_Q3_2026_11_02.csv` produces rows
`1201200,NANPA,0.0012,0.0012,0.0012,0,12,6,,` / `1201201,NANPA,0.00234,0.00234,0.00234,0,12,6,,` / `1201202,NANPA,0.0012,0.0012,0.0012,0,12,6,,`
(identical to `rate_template_npanxx.csv`; effective_date blank only because the BLC email/file date handling is decided by the rules above — if the file has a date column, it IS filled).
Also test: CONVO and Dial World 6060 produce identical prefix/inter/intra values for shared prefixes.

## 5. Architecture
```
dummy/
  config.py        .env + paths
  portal.py        Playwright client: login, carrier_trunks, download_template, create_rate_deck, upload  (ONLY place with selectors)
  parse.py         pure HTML parsing
  scrape_trunks.py CLI: portal trunk names -> data/trunks.csv   (DONE in v1)
  cleaner.py       read messy CSV/xlsx/zip -> clean DataFrame of strings
  mapper.py        column aliases + per-carrier profile -> template DataFrame
  validator.py     checks (below) -> list of Issues (error/warn)
  trunk_matcher.py sender->carrier, tech prefix / product name -> trunk; fuzzy (rapidfuzz); returns candidates + confidence
  mailbox.py       IMAP reader, unwrap forwarded messages, attachments, links
  fetcher.py       download attachment/link (requests, Playwright fallback), unzip
  email_llm.py     LLM extraction -> pydantic model (carrier, trunk hints, effective date, increments, deck filename). Rule-based fallback.
  review.py        human-in-the-loop (CLI prompt first; email/Slack/Telegram later)
  store.py         SQLite: messages, runs, decisions, approved_trunks, trunk_config
  orchestrator.py  the pipeline + scheduler
  tests/ + tests/fixtures/
```
Pipeline: `mailbox -> email_llm -> fetcher -> trunk_matcher -> (human if unsure) -> cleaner -> mapper -> validator -> dry-run report -> (human approval if first time) -> portal upload -> audit log + notification`.

LLM use is limited to (a) understanding the email text and (b) *proposing* a column mapping for an **unknown** layout as JSON. The LLM never rewrites CSV rows; pandas does the conversion. Approved mappings are saved per carrier/trunk so later runs are deterministic.

Per-trunk config (YAML or SQLite): `carrier_id, trunk_name, tech_prefix, sender_domains, default_init_duration, default_increment, header_aliases, approved(bool)`.

## 6. Validator checks
Required columns present; prefixes digits-only and consistent length for NPANXX decks (7 digits starting with 1); no duplicate prefixes; rates numeric and >= 0; no empty rates; init_duration/increment are positive integers; row count vs previous deck (warn if drop > 5%); rate change vs previous deck (warn if > X% for many prefixes; X configurable); effective_date parses; output header equals the template exactly. Errors block upload; warnings need human approval.

## 7. Email parsing notes
- Unwrap forwards: the real carrier is the **innermost** `From:`; the humans (founder/NOC) are only forwarders.
- Old notices are often quoted below the new one (Nuso quotes an older `..._10_15.csv.zip`): use the **newest** attachment/date.
- Dial World emails have a **"Download Now" link** (valid until a stated date) plus Tech Prefix and Trunk name; use the tech prefix to match the trunk.
- ANI emails name **no trunk** -> map by sender domain to carrier, then ask a human.
- Nuso: unlisted NPANXX bill at a default rate ($0.06) — surface to the human, do not invent a row.
- Flag billing-increment changes mentioned in emails (e.g. "6/6 -> 12/6 effective 2026-11-01").

## 8. Coding conventions
Python 3.11+, type hints, small functions, `pandas` (all `dtype=str`), `pydantic` v2 for structured data, `pytest` for tests, `logging` (no prints of secrets). Every module gets tests using fixtures; no test may hit the live portal or email (mock them). Keep dependencies minimal and listed in `requirements.txt`.

## 8b. How to work (for the AI tool)
1. Restate the milestone's acceptance criteria. 2. Write/extend tests first. 3. Implement. 4. Run `pytest`, fix, repeat. 5. Summarize what changed and what is still uncertain. Do not refactor unrelated modules. If a requirement conflicts with this file, **ask** instead of choosing.

## 9. Milestones & acceptance criteria
- **M1 Trunk scraper** (v1 exists): tune `portal.py`/`parse.py` to the real pages using `data/snapshots/*.html`. Done when `python scrape_trunks.py --ids 1-120` produces a correct `data/trunks.csv` (carrier name + trunk names).
- **M2 Cleaner + mapper + validator** (offline, no portal): `python convert.py <file> --trunk-config X -o out.csv`. Done when all 5 fixtures convert, the BLC test in section 4 passes, CONVO == Dial World on shared prefixes, and validator reports blank-row/duplicate/bad-rate cases in synthetic tests.
- **M3 Trunk matcher + store + review CLI**: given an email's extracted hints and `trunks.csv`, returns best trunk with confidence; below threshold or no match -> review prompt. Done when matching for Dial World (tech prefix) and the ambiguous ANI case behave as described in section 7.
- **M4 Portal writer (dry-run first)**: `python upload.py <converted.csv> --trunk "BTCL TNT TRUNK"` prints exactly what it would do (deck name, LRN checkbox, file); `--commit` performs it. Verify on BTCL TNT TRUNK only.
- **M5 Mailbox + fetcher**: IMAP polling, forward-unwrapping, attachment/zip/link download, idempotency by Message-ID. Tested with saved `.eml` fixtures.
- **M6 Email understanding (LLM)**: pydantic-validated extraction with rule-based fallback; tested on the 4 sample emails in `tests/fixtures/emails/`.
- **M7 Orchestrator**: scheduler, notifications ("BTCL TNT TRUNK 2026-11-03: 12,400 rows, 31 changes"), audit log, `Dockerfile`, README for VPS deployment.

## 10. Open questions (do not guess; ask the owner)
1. Exact `effective_date` format the portal wants (or whether it is ignored).
2. `grace_time`: blank vs `0`.
3. Where default 12/6 should come from per carrier when the file lacks it (trunk config vs email).
4. Portal upload mechanics on `rate_deck_upload.php` (confirmation step? 2FA/CAPTCHA?).
5. Is the portal restricted by IP (matters for VPS deployment)?

### Resolved (owner)
- `effective_date`: ISO `YYYY-MM-DD` in the CSV and the portal; deck names use `MM_DD_YYYY`. If the email
  date and the file's date column disagree, Dummy emits a **warning** (which one wins is still open).
- `grace_time` when the carrier omits it -> **blank**.
- Default init/increment when the file lacks them -> **12/6**.
- Upload confirmation / CAPTCHA -> **none**.
- IP restriction -> **still unknown** (blocks the VPS decision).
