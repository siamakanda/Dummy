# Prompts to paste into OpenCode / Command Code (one per milestone)

Setup once: open the project folder in the tool, make sure `AGENTS.md` is in the root, `tests/fixtures/` has the sample files, `.env` exists.
If the tool does not auto-read AGENTS.md, start every session with: "Read AGENTS.md fully and follow it."

## M1 — tune the scraper (needs your real portal)
Read AGENTS.md. Run `python scrape_trunks.py --discover --headed` against the real portal. Open the saved snapshots in data/snapshots and fix
portal.py (login selectors) and parse.py (trunk table detection) until `python scrape_trunks.py --ids 41 --headed` writes the right trunk names to
data/trunks.csv. Add a test using the saved carrier_41.html (remove private data first). Do not touch other modules.

## M2 — cleaner, mapper, validator
Read AGENTS.md sections 3, 4 and 6. Implement cleaner.py, mapper.py, validator.py and a CLI convert.py. Write tests FIRST in tests/test_convert.py
using tests/fixtures. Required tests: the BLC verified expectation, CONVO == Dial World 6060 on shared prefixes, Fractel '#' header detection,
BOM handling, blank-row removal, indeterminate = MAX(inter, intra) when missing, destination always NANPA, rates stay exact strings. Run pytest until green. No portal or email code.

## M3 — trunk matcher, store, review
Read AGENTS.md sections 5 and 7. Implement store.py (SQLite), trunk_matcher.py (sender domain -> carrier, tech prefix/product name -> trunk, rapidfuzz, confidence score)
and review.py (CLI prompt). Test with data/trunks.csv-style fixtures and these cases: Dial World prefix 6060 -> 'USA CONVO'; ANI email with no trunk -> must ask a human; unknown trunk -> must ask a human.

## M4 — portal writer (dry-run)
Read AGENTS.md sections 2 and 6. Add create_rate_deck/upload to portal.py and an upload.py CLI that is DRY-RUN by default and prints the plan (name '<Trunk> <date>', 'Use LRN for billing' ticked, file path).
Only with --commit does it act, and only for trunk 'BTCL TNT TRUNK' during development. Run headed first and show me screenshots before any commit.

## M5 — mailbox + fetcher
Read AGENTS.md section 7. Implement mailbox.py (IMAP, idempotent by Message-ID, unwrap nested forwards to find the original sender) and fetcher.py (attachment, zip, link download with Playwright fallback).
Test with .eml fixtures in tests/fixtures/emails/ (create them from the four sample emails in AGENTS.md section 7; no live mailbox in tests).

## M6 — email understanding
Implement email_llm.py: extract carrier, trunk hints (trunk name, tech prefix, product), effective date, billing increments, deck filename into a pydantic model using the Anthropic API,
with a rule-based fallback and strict JSON validation. Tests on the four sample emails; mock the API in tests.

## M7 — orchestrator
Implement orchestrator.py wiring all modules per the pipeline in AGENTS.md section 5, scheduler, notifications, audit log, Dockerfile, README for VPS deployment. Add an end-to-end test using mocks (no live portal/email).

---
## Tips for working with a weaker model (e.g. DeepSeek)
- Keep each session to ONE milestone; start a fresh session for the next.
- Always make it run `pytest` and paste you the failures; do not accept "should work".
- If it invents requirements, point it at the AGENTS.md section and tell it to stop and ask.
- Paste the diff / test output here for me to review before you move to the next milestone, especially M4 (anything that writes to the portal).
