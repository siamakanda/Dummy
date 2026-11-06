"""Dummy core engine - folder-first, Windows-local (the primary interface).

The operator drops carrier rate decks into ``inbox/``. Dummy lists them next to the switch trunk
names, the human maps each file to a trunk (CLI here, or the local ``engine_web`` dashboard), then
Dummy converts to our template, validates, dry-runs, and - with ``--commit`` on the dev trunk only -
pushes the deck to the portal.

    python engine.py sync                 # read trunks from the switch -> SQLite (+ data/trunks.csv)
    python engine.py scan                 # scan inbox/ -> one job per deck (with suggestions)
    python engine.py list --trunks        # show jobs + the switch trunks
    python engine.py map 3 "BTCL TNT TRUNK"
    python engine.py run                  # convert + validate mapped jobs (dry run)
    python engine.py upload               # step 1: create the deck + upload the CSV (--commit to write)
    python engine.py assign               # step 2: assign an uploaded deck to its trunk (--commit)

Everything reuses the existing modules (AGENTS.md sections 2-6); no portal write happens without
``--commit``.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import json
import logging
from pathlib import Path

import config
from cleaner import read_clean
from input_sources import DirectorySource
from mapper import missing_required_columns, parse_date_from_filename, parse_effective_date, to_template
from store import Store
from trunk_matcher import match_trunks
from upload import build_deck_name, build_plan, commit_allowed, find_trunk
from validator import validate

log = logging.getLogger("dummy.engine")


def _audit(record: dict) -> None:
    """Append one line to data/audit.jsonl (never let logging break a write)."""
    try:
        config.AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(config.AUDIT_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("audit write failed")


def cmd_sync(store: Store, args) -> int:
    """Read carriers/trunks from the switch into the store (+ data/trunks.csv)."""
    from parse import carrier_name, extract_carriers, extract_trunks
    from portal import Portal

    out_path = config.DATA_DIR / "trunks.csv"
    with Portal(headless=not args.headed) as portal:
        ids = [c["carrier_id"] for c in extract_carriers(portal.carriers_html())]
        print(f"discovered {len(ids)} carriers from /carriers.php")
        rows = []
        for cid in ids:
            html = portal.carrier_trunks_html(cid)
            trunks = extract_trunks(html)
            if not trunks:
                continue
            name = carrier_name(html, f"carrier_{cid}")
            for trunk in trunks:
                rows.append({"carrier_id": cid, "carrier_name": name, **trunk})
            print(f"  carrier {cid} ({name}): {len(trunks)} trunks")
    fields = ["carrier_id", "carrier_name", "trunk_id", "trunk_name", "prefix", "status", "cost_deck"]
    with open(out_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    loaded = store.load_trunks(out_path)
    print(f"loaded {loaded} trunks into {store.path}")
    return 0


def scan_inbox(store: Store, folder: str | Path | None = None) -> tuple[int, int]:
    """Scan the inbox folder for decks and create a job per file (with suggestions)."""
    inbox = Path(folder or config.INBOX_DIR)
    inbox.mkdir(parents=True, exist_ok=True)
    work_root = config.ARCHIVE_DIR / dt.date.today().isoformat()
    items = DirectorySource(inbox).fetch(work_root)
    log.info("Scanning %s (%d file(s))...", inbox, len(items))
    created = 0
    for item in items:
        deck = item.deck_files[0] if item.deck_files else None
        match = match_trunks(item.facts.to_hints(), store.all_trunks())
        candidates = [{"trunk_id": c.trunk_id, "trunk_name": c.trunk_name, "score": c.score}
                      for c in match.candidates[:5]]
        job_id = store.add_job(item.source_id, file_path=str(deck or ""),
                               file_name=(deck.name if deck else item.source_id),
                               candidates=candidates, effective_date=item.facts.effective_date,
                               date_hint=parse_date_from_filename(deck.name if deck else item.source_id),
                               date_source=("auto" if item.facts.effective_date else "none"))
        if job_id:
            created += 1
            log.info("Added %s", deck.name if deck else item.source_id)
            print(f"  job {job_id}: {deck.name if deck else '?'}  "
                  f"suggestions={[c['trunk_name'] for c in candidates] or 'none'}")
    log.info("Scan complete: %d scanned, %d new", len(items), created)
    return len(items), created


def cmd_scan(store: Store, args) -> int:
    scanned, created = scan_inbox(store, args.dir)
    print(f"scanned {scanned} file(s); {created} new job(s)")
    return 0


def cmd_list(store: Store, args) -> int:
    jobs = store.list_jobs(args.status or None)
    if not jobs:
        print("no jobs - drop decks in inbox/ then run: python engine.py scan")
        return 0
    for job in jobs:
        trunk = store.get_trunk(job["trunk_id"]) if job["trunk_id"] else None
        label = trunk["trunk_name"] if trunk else "(unmapped)"
        print(f"#{job['job_id']:<3} {job['status']:<15} {label:<28} {job['file_name']}")
        if job["candidates"]:
            suggestions = ", ".join(f"{c['trunk_name']} ({c['score']:.2f})" for c in job["candidates"][:3])
            print(f"      suggestions: {suggestions}")
    if getattr(args, "trunks", False):
        print("\nswitch trunks:")
        for trunk in store.all_trunks():
            print(f"  {trunk['trunk_id']:<4} {trunk['trunk_name']}")
    return 0


def cmd_map(store: Store, args) -> int:
    if store.get_job(args.job) is None:
        print(f"no job #{args.job}")
        return 2
    trunk = find_trunk(store, args.trunk)
    if trunk is None:
        print(f"trunk '{args.trunk}' not found")
        return 2
    store.set_job_mapping(args.job, int(trunk["trunk_id"]))
    print(f"job #{args.job} -> {trunk['trunk_name']}")
    return 0


def _previous_deck(store: Store, trunk_id: int, job_id: int):
    """Load the last converted deck for the trunk (deck_history, then deck_jobs) for comparison."""
    paths: list[str] = []
    last = store.last_deck(trunk_id)
    if last and last.get("converted_path"):
        paths.append(last["converted_path"])
    prior = store.previous_deck_path(trunk_id, exclude_job_id=job_id)
    if prior:
        paths.append(prior)
    for path in paths:
        if path and Path(path).exists():
            try:
                return read_clean(path)
            except Exception as exc:                 # never let a bad previous deck block conversion
                log.warning("Could not read previous deck %s: %s", path, exc)
    return None


def convert_job(store: Store, job: dict, *, today: dt.date | None = None) -> dict:
    """Read -> template -> validate for one mapped job; updates the job row."""
    path = Path(job["file_path"])
    if not path.exists():
        store.update_job(job["job_id"], status="failed", notes=f"missing file {path}")
        return {"status": "failed", "error": f"missing file {path}"}
    log.info("Converting %s ...", path.name)
    config_for_trunk = store.get_config(int(job["trunk_id"])) or {}
    raw = read_clean(path)
    missing = missing_required_columns(list(raw.columns), config_for_trunk.get("header_aliases"))
    if missing:
        log.warning("Unknown layout for %s: unmapped columns %s", path.name, missing)
        suggestion = _propose_mapping(raw)
        note = f"unmapped columns: {missing}"
        if suggestion:
            note += " - AI suggested a mapping; apply it from the dashboard"
        store.update_job(job["job_id"], status="needs_mapping", notes=note,
                         suggested_aliases=json.dumps(suggestion) if suggestion else None)
        return {"status": "needs_mapping", "missing": missing, "suggestion": suggestion}
    if not job.get("effective_date"):
        log.warning("No effective date for %s - set it before uploading", path.name)
        store.update_job(job["job_id"], status="needs_date",
                         notes="no effective date - set it on the dashboard")
        return {"status": "needs_date"}
    frame = to_template(raw, config_for_trunk)
    previous = _previous_deck(store, int(job["trunk_id"]), job["job_id"])
    log.info("  %d rows - validating ...", len(frame))
    issues = validate(frame, previous=previous)
    errors = [str(issue) for issue in issues if issue.level == "error"]
    warnings = [str(issue) for issue in issues if issue.level == "warn"]
    trunk = store.get_trunk(int(job["trunk_id"]))
    deck_name = build_deck_name(trunk["trunk_name"], job["effective_date"], today)
    if deck_name is None:
        store.update_job(job["job_id"], status="needs_date",
                         notes=f"effective date '{job['effective_date']}' is not a valid date")
        return {"status": "needs_date"}
    converted = path.parent / f"{deck_name}.csv"
    frame.to_csv(converted, index=False, encoding="utf-8")
    if errors:
        log.warning("Validation failed for %s (%d error(s))", path.name, len(errors))
        store.update_job(job["job_id"], status="validation_error", rows=len(frame),
                         converted_path=str(converted), notes="; ".join(errors[:3]))
        return {"status": "validation_error", "errors": errors, "rows": len(frame)}
    log.info("  ready: %s (%d warning(s))", deck_name, len(warnings))
    store.update_job(job["job_id"], status="dry_run", rows=len(frame), converted_path=str(converted),
                     deck_name=deck_name, notes=f"{len(warnings)} warning(s)")
    return {"status": "dry_run", "rows": len(frame), "warnings": warnings,
            "converted_path": str(converted), "deck_name": deck_name}


def _propose_mapping(raw):
    """Ask the LLM for header aliases (validated against the real headers); {} if unavailable."""
    try:
        from email_llm import propose_mapping
        return propose_mapping(list(raw.columns), raw.head(5).to_dict("records")) or {}
    except Exception as exc:
        log.warning("AI mapping suggestion unavailable: %s", exc)
        return {}


def apply_suggestion(store: Store, job_id: int) -> dict:
    """Write the stored AI alias suggestion into the trunk config, then re-convert the deck."""
    job = store.get_job(job_id)
    if not job:
        return {"status": "failed", "error": "no such job"}
    suggestion = job.get("suggested_aliases") or {}
    if not suggestion:
        return {"status": "failed", "error": "no suggested mapping for this deck"}
    trunk_id = int(job["trunk_id"])
    cfg = store.get_config(trunk_id) or {}
    merged = {**(cfg.get("header_aliases") or {}), **suggestion}
    store.set_config(trunk_id, carrier_id=cfg.get("carrier_id"), header_aliases=merged)
    store.update_job(job_id, status="mapped", suggested_aliases=None)
    result = convert_job(store, store.get_job(job_id))
    return {"job_id": job_id, **result}


def apply_date(store: Store, job_id: int, value: str) -> dict:
    """Set the job's effective date (manual override) and re-convert the deck."""
    job = store.get_job(job_id)
    if not job:
        return {"status": "failed", "error": "no such job"}
    iso = parse_effective_date(str(value or "").strip())
    try:
        dt.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return {"status": "failed", "error": "invalid date - use YYYY-MM-DD"}
    store.update_job(job_id, effective_date=iso, date_source="manual", status="mapped")
    return {"job_id": job_id, **convert_job(store, store.get_job(job_id))}


def run_mapped(store: Store, job_id: int | None = None) -> list[dict]:
    """Convert + validate the mapped job(s) (or one job); returns per-job results."""
    jobs = [store.get_job(job_id)] if job_id else store.list_jobs("mapped")
    results = []
    for job in [job for job in jobs if job]:
        results.append({"job_id": job["job_id"], "file_name": job["file_name"],
                        **convert_job(store, job)})
    return results


def upload_job(store: Store, portal, job: dict) -> dict:
    """Step 1: create the deck and upload the rates. Does NOT touch a trunk, so any trunk is allowed."""
    if job.get("deck_id"):
        return {"job_id": job["job_id"], "status": "skipped",
                "reason": f"deck {job['deck_id']} is already uploaded"}
    if job.get("status") != "dry_run":
        return {"job_id": job["job_id"], "status": "skipped",
                "reason": f"job is '{job['status']}' - convert it first"}
    if not job.get("effective_date"):
        return {"job_id": job["job_id"], "status": "skipped",
                "reason": "set the effective date first"}
    trunk = store.get_trunk(int(job["trunk_id"]))
    try:
        plan = build_plan(store, trunk["trunk_name"], job["converted_path"],
                          effective_date=job["effective_date"])
    except ValueError as exc:
        return {"job_id": job["job_id"], "status": "skipped", "reason": str(exc)}
    if job.get("deck_name"):
        plan.deck_name = job["deck_name"]
    deck_id = None
    try:
        log.info("Creating deck '%s' ...", plan.deck_name)
        deck_id = portal.create_rate_deck(plan.deck_name, description=trunk["trunk_name"],
                                          lrn_billing=True)
        log.info("Uploading %s row(s) ...", plan.rows)
        upload = portal.upload_rate_deck(deck_id, plan.csv_path,
                                         effective_date=plan.effective_date or None, replace=True)
        if not upload.get("ok"):
            raise RuntimeError("upload reported not ok")
    except Exception as exc:
        log.error("Upload failed: %s", exc)
        if deck_id is not None:                       # created but upload failed -> remove the orphan
            try:
                portal.delete_rate_deck(deck_id)
            except Exception:
                pass
        store.update_job(job["job_id"], status="failed", notes=str(exc))
        return {"job_id": job["job_id"], "status": "failed", "error": str(exc)}
    created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    store.update_job(job["job_id"], status="uploaded", deck_id=deck_id, deck_name=plan.deck_name,
                     deck_created_at=created_at,
                     notes=f"deck {deck_id} '{plan.deck_name}' created {created_at} - not yet assigned")
    _audit({"event": "upload", "job_id": job["job_id"], "file": job["file_name"],
            "trunk_id": trunk["trunk_id"], "trunk": trunk["trunk_name"],
            "deck_id": deck_id, "deck_name": plan.deck_name, "rows": plan.rows,
            "deck_created_at": created_at})
    return {"job_id": job["job_id"], "status": "uploaded", "deck_id": deck_id,
            "deck_name": plan.deck_name}


def assign_job(store: Store, portal, job: dict) -> dict:
    """Step 2: assign the already-uploaded deck to its trunk and verify."""
    if job.get("status") == "assigned":
        return {"job_id": job["job_id"], "status": "skipped", "reason": "already assigned"}
    trunk = store.get_trunk(int(job["trunk_id"]))
    deck_id = job.get("deck_id")
    if not deck_id:
        store.update_job(job["job_id"], notes="no uploaded deck recorded - upload first")
        return {"job_id": job["job_id"], "status": "failed",
                "error": "no uploaded deck recorded - upload the deck first"}
    allowed, reason = commit_allowed(store, trunk)      # assigning changes billing -> gate it
    if not allowed:
        return {"job_id": job["job_id"], "status": "refused", "reason": reason}
    deck_name = job.get("deck_name") or build_deck_name(trunk["trunk_name"], job.get("effective_date"))
    log.info("Assigning deck %s to %s ...", deck_id, trunk["trunk_name"])
    try:
        assigned = portal.assign_deck_to_trunk(trunk["carrier_id"], trunk["trunk_id"], deck_id,
                                               expect_name=deck_name)
        if not assigned.get("ok"):
            raise RuntimeError("assign to trunk failed")
    except Exception as exc:
        log.error("Assign failed: %s", exc)
        store.update_job(job["job_id"], status="uploaded", notes=f"assign failed: {exc}")
        return {"job_id": job["job_id"], "status": "failed", "error": str(exc), "deck_id": deck_id}
    log.info("Verified: deck %s is assigned to %s", deck_id, trunk["trunk_name"])
    store.update_job(job["job_id"], status="assigned", deck_id=deck_id, deck_name=deck_name,
                     notes=f"deck {deck_id} '{deck_name}' assigned to {trunk['trunk_name']}")
    store.record_deck(int(trunk["trunk_id"]), job.get("effective_date"), int(job.get("rows") or 0),
                      0, job.get("converted_path"), deck_id=deck_id)
    _audit({"event": "assign", "job_id": job["job_id"], "file": job["file_name"],
            "trunk_id": trunk["trunk_id"], "trunk": trunk["trunk_name"],
            "deck_id": deck_id, "deck_name": deck_name})
    return {"job_id": job["job_id"], "status": "assigned", "deck_id": deck_id, "deck_name": deck_name}


def verify_deck(store: Store, portal, job: dict) -> dict:
    """Read-only: confirm the recorded deck exists on the switch and report its real name."""
    deck_id = job.get("deck_id")
    if not deck_id:
        return {"status": "unknown", "error": "no deck recorded for this job - upload first"}
    try:
        decks = portal.list_decks()
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    found = next((d for d in decks if str(d.get("deck_id")) == str(deck_id)), None)
    expected = job.get("deck_name") or ""
    if not found:
        return {"status": "missing", "deck_id": deck_id, "expected": expected}
    actual = found.get("name", "")
    return {"status": "found", "deck_id": deck_id, "name": actual, "expected": expected,
            "name_matches": actual.strip() == expected.strip(),
            "portal_status": found.get("status", "")}


def redo_upload(store: Store, portal, job: dict) -> dict:
    """Delete an uploaded (unassigned) deck and reset the job so it can be uploaded again."""
    deck_id = job.get("deck_id")
    if not deck_id:
        return {"job_id": job["job_id"], "status": "skipped", "reason": "nothing uploaded to redo"}
    if job.get("status") == "assigned":
        return {"job_id": job["job_id"], "status": "refused",
                "reason": "deck is assigned to a trunk - unassign it first"}
    try:
        if portal.deck_is_assigned(deck_id):
            return {"job_id": job["job_id"], "status": "refused",
                    "reason": "deck is assigned to a trunk - refusing to delete it"}
    except Exception as exc:
        return {"job_id": job["job_id"], "status": "failed",
                "error": f"could not check assignment: {exc}"}
    try:
        portal.delete_rate_deck(deck_id)
    except Exception as exc:
        return {"job_id": job["job_id"], "status": "failed", "error": str(exc)}
    has_date = bool(job.get("effective_date"))
    store.update_job(job["job_id"], deck_id=None, deck_name=None, deck_created_at=None,
                     status=("mapped" if has_date else "needs_date"),
                     notes=f"redo: deleted deck {deck_id}")
    _audit({"event": "redo", "job_id": job["job_id"], "file": job["file_name"],
            "deck_id": deck_id, "trunk_id": job.get("trunk_id")})
    if not has_date:
        return {"job_id": job["job_id"], "status": "needs_date", "deleted": deck_id}
    result = convert_job(store, store.get_job(job["job_id"]))
    return {"job_id": job["job_id"], "status": result.get("status"), "deleted": deck_id,
            **result}


def upload_jobs(store: Store, portal, *, commit: bool, job_id: int | None = None) -> list[dict]:
    """Upload step for dry_run job(s); dry-run prints the plan."""
    jobs = [store.get_job(job_id)] if job_id else store.list_jobs("dry_run")
    results = []
    for job in [job for job in jobs if job]:
        if not commit:
            trunk = store.get_trunk(int(job["trunk_id"]))
            plan = build_plan(store, trunk["trunk_name"], job["converted_path"])
            results.append({"job_id": job["job_id"], "status": "dry_run",
                            "deck_name": plan.deck_name, "rows": plan.rows})
        else:
            results.append(upload_job(store, portal, job))
    return results


def assign_jobs(store: Store, portal, *, commit: bool, job_id: int | None = None) -> list[dict]:
    """Assign step for uploaded job(s)."""
    jobs = [store.get_job(job_id)] if job_id else store.list_jobs("uploaded")
    results = []
    for job in [job for job in jobs if job]:
        if not commit:
            results.append({"job_id": job["job_id"], "status": "uploaded",
                            "deck_id": job.get("deck_id"), "deck_name": job.get("deck_name")})
        else:
            results.append(assign_job(store, portal, job))
    return results


def cmd_run(store: Store, args) -> int:
    results = run_mapped(store, args.job)
    if not results:
        print('no mapped jobs - map first: python engine.py map <job_id> "<trunk>"')
        return 0
    for item in results:
        extra = f" ({item.get('rows')} rows)" if item.get("rows") else ""
        print(f"#{item['job_id']} {item['file_name']}: {item['status']}{extra}")
    return 0


def cmd_upload(store: Store, args) -> int:
    portal_ctx = contextlib.nullcontext(None)
    if args.commit:
        from portal import Portal
        portal_ctx = Portal(headless=not args.headed)
    with portal_ctx as portal:
        results = upload_jobs(store, portal, commit=args.commit, job_id=args.job)
    if not results:
        print("nothing to upload (no dry_run jobs) - run: python engine.py run")
        return 0
    for item in results:
        if item["status"] == "refused":
            print(f"#{item['job_id']}: refused - {item['reason']}")
        elif item["status"] == "dry_run":
            print(f"DRY RUN #{item['job_id']} -> {item['deck_name']} ({item['rows']} rows) [add --commit]")
        elif item["status"] == "skipped":
            print(f"#{item['job_id']}: skipped - {item.get('reason', '')}")
        else:
            print(f"#{item['job_id']} {item.get('deck_name', '')}: {item['status']} "
                  f"deck={item.get('deck_id')} {item.get('error', '')}".rstrip())
    return 0


def cmd_assign(store: Store, args) -> int:
    portal_ctx = contextlib.nullcontext(None)
    if args.commit:
        from portal import Portal
        portal_ctx = Portal(headless=not args.headed)
    with portal_ctx as portal:
        results = assign_jobs(store, portal, commit=args.commit, job_id=args.job)
    if not results:
        print("nothing to assign (no uploaded jobs) - upload a deck first")
        return 0
    for item in results:
        if not args.commit:
            print(f"DRY RUN #{item['job_id']}: deck {item.get('deck_id')} "
                  f"{item.get('deck_name') or ''} [add --commit]")
        elif item["status"] == "assigned":
            print(f"#{item['job_id']} {item.get('deck_name', '')}: assigned deck={item.get('deck_id')}")
        elif item["status"] == "skipped":
            print(f"#{item['job_id']}: skipped - {item.get('reason', '')}")
        else:
            print(f"#{item['job_id']} {item.get('deck_name', '')}: {item['status']} "
                  f"{item.get('error', '')}".rstrip())
    return 0


def cmd_redo(store: Store, args) -> int:
    job = store.get_job(args.job)
    if not job or not job.get("deck_id"):
        print(f"nothing to redo for job #{args.job} (no uploaded deck)")
        return 0
    if not args.commit:
        print(f"DRY RUN #{args.job}: would delete deck {job['deck_id']} "
              f"({job.get('deck_name') or 'unnamed'}) and reset the job [add --commit]")
        return 0
    from portal import Portal
    with Portal(headless=not args.headed) as portal:
        result = redo_upload(store, portal, job)
    if result["status"] in ("dry_run", "mapped", "needs_date"):
        print(f"#{args.job}: deleted deck {result.get('deleted')} -> {result['status']} "
              f"(upload again when ready)")
        return 0
    print(f"#{args.job}: {result['status']} - {result.get('reason') or result.get('error') or ''}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dummy core engine (folder-first).")
    parser.add_argument("--db", default=str(config.DB_PATH))
    parser.add_argument("--headed", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="read trunks from the switch into the store")
    p_scan = sub.add_parser("scan", help="scan inbox/ for decks -> jobs")
    p_scan.add_argument("--dir", help="folder to scan (default INBOX_DIR)")
    p_list = sub.add_parser("list", help="list jobs (and optionally the trunks)")
    p_list.add_argument("--status")
    p_list.add_argument("--trunks", action="store_true")
    p_map = sub.add_parser("map", help="map a job to a trunk")
    p_map.add_argument("job", type=int)
    p_map.add_argument("trunk")
    p_run = sub.add_parser("run", help="convert + validate mapped jobs (dry run)")
    p_run.add_argument("--job", type=int)
    p_upload = sub.add_parser("upload", help="step 1: create the deck + upload the CSV (no assign)")
    p_upload.add_argument("--job", type=int)
    p_upload.add_argument("--commit", action="store_true")
    p_assign = sub.add_parser("assign", help="step 2: assign an uploaded deck to its trunk")
    p_assign.add_argument("--job", type=int)
    p_assign.add_argument("--commit", action="store_true")
    p_redo = sub.add_parser("redo", help="delete an uploaded (unassigned) deck and reset the job")
    p_redo.add_argument("--job", type=int, required=True)
    p_redo.add_argument("--commit", action="store_true")

    args = parser.parse_args(argv)
    Path(config.INBOX_DIR).mkdir(parents=True, exist_ok=True)
    with Store.open(args.db) as store:
        return {"sync": cmd_sync, "scan": cmd_scan, "list": cmd_list,
                "map": cmd_map, "run": cmd_run, "upload": cmd_upload,
                "assign": cmd_assign, "redo": cmd_redo}[args.command](store, args)


if __name__ == "__main__":
    raise SystemExit(main())
