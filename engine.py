"""Dummy core engine - folder-first, Windows-local (ACTIVE on the ``engine`` branch).

The operator drops carrier rate decks into ``inbox/``. Dummy lists them next to the switch trunk
names, the human maps each file to a trunk (CLI here, or the local ``engine_web`` dashboard), then
Dummy converts to our template, validates, dry-runs, and - with ``--commit`` on the dev trunk only -
pushes the deck to the portal.

    python engine.py sync                 # read trunks from the switch -> SQLite (+ data/trunks.csv)
    python engine.py scan                 # scan inbox/ -> one job per deck (with suggestions)
    python engine.py list --trunks        # show jobs + the switch trunks
    python engine.py map 3 "BTCL TNT TRUNK"
    python engine.py run                  # convert + validate mapped jobs (dry run)
    python engine.py push                 # dry-run the upload (add --commit to write; dev trunk only)

Everything reuses the existing modules (AGENTS.md sections 2-6); no portal write happens without
``--commit``.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import logging
from pathlib import Path

import config
from cleaner import read_clean
from input_sources import DirectorySource
from mapper import to_template
from store import Store
from trunk_matcher import match_trunks
from upload import build_deck_name, build_plan, commit_allowed, find_trunk
from validator import validate

log = logging.getLogger("dummy.engine")


def _open_store(db_path: str) -> Store:
    store = Store(db_path)
    if not store.all_trunks() and (config.DATA_DIR / "trunks.csv").exists():
        store.load_trunks(config.DATA_DIR / "trunks.csv")
    return store


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
                               candidates=candidates, effective_date=item.facts.effective_date)
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


def convert_job(store: Store, job: dict, *, today: dt.date | None = None) -> dict:
    """Read -> template -> validate for one mapped job; updates the job row."""
    path = Path(job["file_path"])
    if not path.exists():
        store.update_job(job["job_id"], status="failed", notes=f"missing file {path}")
        return {"status": "failed", "error": f"missing file {path}"}
    log.info("Converting %s ...", path.name)
    config_for_trunk = store.get_config(int(job["trunk_id"])) or {}
    frame = to_template(read_clean(path), config_for_trunk)
    log.info("  %d rows - validating ...", len(frame))
    issues = validate(frame)
    errors = [str(issue) for issue in issues if issue.level == "error"]
    warnings = [str(issue) for issue in issues if issue.level == "warn"]
    trunk = store.get_trunk(int(job["trunk_id"]))
    deck_name = build_deck_name(trunk["trunk_name"], job["effective_date"], today)
    converted = path.parent / f"{deck_name}.csv"
    frame.to_csv(converted, index=False, encoding="utf-8")
    if errors:
        log.warning("Validation failed for %s (%d error(s))", path.name, len(errors))
        store.update_job(job["job_id"], status="validation_error", rows=len(frame),
                         converted_path=str(converted), notes="; ".join(errors[:3]))
        return {"status": "validation_error", "errors": errors, "rows": len(frame)}
    log.info("  ready: %s (%d warning(s))", deck_name, len(warnings))
    store.update_job(job["job_id"], status="dry_run", rows=len(frame), converted_path=str(converted),
                     notes=f"{len(warnings)} warning(s)")
    return {"status": "dry_run", "rows": len(frame), "warnings": warnings,
            "converted_path": str(converted), "deck_name": deck_name}


def run_mapped(store: Store, job_id: int | None = None) -> list[dict]:
    """Convert + validate the mapped job(s) (or one job); returns per-job results."""
    jobs = [store.get_job(job_id)] if job_id else store.list_jobs("mapped")
    results = []
    for job in [job for job in jobs if job]:
        results.append({"job_id": job["job_id"], "file_name": job["file_name"],
                        **convert_job(store, job)})
    return results


def commit_job(store: Store, portal, job: dict) -> dict:
    """create -> upload -> assign -> verify for one job, with orphan cleanup on any failure."""
    trunk = store.get_trunk(int(job["trunk_id"]))
    allowed, reason = commit_allowed(store, trunk)
    if not allowed:
        return {"job_id": job["job_id"], "status": "refused", "reason": reason}
    plan = build_plan(store, trunk["trunk_name"], job["converted_path"])
    deck_id = None
    created = False
    try:
        log.info("Creating deck '%s' ...", plan.deck_name)
        deck_id = portal.create_rate_deck(plan.deck_name, description=trunk["trunk_name"],
                                          lrn_billing=True)
        created = True
        log.info("Uploading %s row(s) ...", plan.rows)
        upload = portal.upload_rate_deck(deck_id, plan.csv_path,
                                         effective_date=plan.effective_date or None, replace=True)
        if not upload.get("ok"):
            raise RuntimeError("upload reported not ok")
        log.info("Assigning deck %s to %s ...", deck_id, trunk["trunk_name"])
        assigned = portal.assign_deck_to_trunk(trunk["carrier_id"], trunk["trunk_id"], deck_id,
                                               expect_name=plan.deck_name)
        if not assigned.get("ok"):
            raise RuntimeError("assign to trunk failed")
        log.info("Verified: deck %s is assigned to %s", deck_id, trunk["trunk_name"])
    except Exception as exc:
        log.error("Push failed: %s", exc)
        if created and deck_id is not None:                 # never leave an orphan behind
            try:
                portal.delete_rate_deck(deck_id)
            except Exception:
                pass
        store.update_job(job["job_id"], status="failed", notes=str(exc))
        return {"job_id": job["job_id"], "status": "failed", "error": str(exc)}
    store.update_job(job["job_id"], status="uploaded",
                     notes=f"deck {deck_id} uploaded and assigned to {trunk['trunk_name']}")
    return {"job_id": job["job_id"], "status": "uploaded", "deck_id": deck_id,
            "deck_name": plan.deck_name}


def push_jobs(store: Store, portal, *, commit: bool, job_id: int | None = None) -> list[dict]:
    """Push dry_run job(s); writes only when ``commit`` is set and the trunk is allowed."""
    jobs = [store.get_job(job_id)] if job_id else store.list_jobs("dry_run")
    results = []
    for job in [job for job in jobs if job]:
        if commit:
            results.append(commit_job(store, portal, job))
            continue
        trunk = store.get_trunk(int(job["trunk_id"]))
        plan = build_plan(store, trunk["trunk_name"], job["converted_path"])
        results.append({"job_id": job["job_id"], "status": "dry_run",
                        "deck_name": plan.deck_name, "rows": plan.rows})
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


def cmd_push(store: Store, args) -> int:
    portal_ctx = contextlib.nullcontext(None)
    if args.commit:
        from portal import Portal
        portal_ctx = Portal(headless=not args.headed)
    with portal_ctx as portal:
        results = push_jobs(store, portal, commit=args.commit, job_id=args.job)
    if not results:
        print("nothing to push (no dry_run jobs) - run: python engine.py run")
        return 0
    for item in results:
        if item["status"] == "refused":
            print(f"#{item['job_id']}: refused - {item['reason']}")
        elif item["status"] == "dry_run":
            print(f"DRY RUN #{item['job_id']} -> {item['deck_name']} ({item['rows']} rows) [add --commit]")
        else:
            print(f"#{item['job_id']} {item.get('deck_name', '')}: {item['status']} "
                  f"deck={item.get('deck_id')} {item.get('error', '')}".rstrip())
    return 0


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
    p_push = sub.add_parser("push", help="upload dry_run jobs (dry-run unless --commit)")
    p_push.add_argument("--job", type=int)
    p_push.add_argument("--commit", action="store_true")

    args = parser.parse_args(argv)
    Path(config.INBOX_DIR).mkdir(parents=True, exist_ok=True)
    with _open_store(args.db) as store:
        return {"sync": cmd_sync, "scan": cmd_scan, "list": cmd_list,
                "map": cmd_map, "run": cmd_run, "push": cmd_push}[args.command](store, args)


if __name__ == "__main__":
    raise SystemExit(main())
