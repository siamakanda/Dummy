"""Dummy orchestrator: the full pipeline (AGENTS.md section 5; M7/M8/M9/M11).

source (email | directory) -> facts -> deck file(s) -> trunk match -> convert -> validate ->
  clean?  -> dry-run report / create+upload+assign+verify
  unclear -> pending queue (human resolves; daemon commits on its next --commit pass)

Multi-deck inputs (one email carrying several trunks' decks) are processed **per deck**, each with
its own trunk match + idempotency key. Dry-run by default; uploads need --commit and the M4 gate.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path

import config
import logging_setup
from cleaner import read_clean
from email_llm import EmailFacts, extract_facts, propose_mapping
from fetcher import fetch_deck_files
from input_sources import DirectorySource, EmailSource, InputItem, safe_name
from mailbox import parse_email
from mapper import LAYOUT_REQUIRED, missing_required_columns, resolve_columns, to_template
from notify import get_notifier
from store import Store
from trunk_matcher import carrier_from_sender, match_trunks
from upload import build_deck_name, commit_allowed, find_trunk
from validator import validate

log = logging.getLogger("dummy.orchestrator")

DECK_SUFFIXES = (".csv", ".xlsx", ".xls")
PREFIX_FILE_RE = re.compile(r"prefix[_\- ]*([0-9]{3,7})", re.IGNORECASE)

def _log_notify(text: str) -> None:
    get_notifier().send(text)

def archive_dir(source_id: str, today: _dt.date | None = None) -> Path:
    day = (today or _dt.date.today()).isoformat()
    return config.ARCHIVE_DIR / day / safe_name(source_id)

def _audit(record: dict) -> None:
    try:
        config.AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(config.AUDIT_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("audit write failed")

def count_changes(previous, current) -> int:
    """Number of prefixes that are new or whose inter_rate changed."""
    if previous is None or len(previous) == 0 or len(current) == 0:
        return 0
    prior = dict(zip(previous["prefix"].astype(str).str.strip(),
                     previous["inter_rate"].astype(str).str.strip()))
    changed = 0
    for prefix, rate in zip(current["prefix"].astype(str).str.strip(),
                            current["inter_rate"].astype(str).str.strip()):
        if prior.get(prefix) != rate:
            changed += 1
    return changed

def _display_date(value: str | None) -> str:
    """Render an ISO date for humans as MM_DD_YYYY (matches the deck-name date)."""
    if not value:
        return ""
    for fmt in (config.EFFECTIVE_DATE_FORMAT, "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(str(value), fmt).strftime(config.DECK_NAME_DATE_FORMAT)
        except ValueError:
            continue
    return str(value)

def _deck_files(files) -> list[Path]:
    return [Path(f) for f in files if Path(f).suffix.lower() in DECK_SUFFIXES]

def _facts_for_file(facts: EmailFacts, path: Path) -> EmailFacts:
    """Per-deck hints: a `Prefix_6060` filename overrides the email's tech prefix."""
    match = PREFIX_FILE_RE.search(path.stem)
    update = {"deck_filename": path.name}
    if match:
        update["tech_prefix"] = match.group(1)
    return facts.model_copy(update=update)

def _sample_rows(df, count: int = 3) -> list[str]:
    return [", ".join(str(value) for value in df.iloc[i].tolist())
            for i in range(min(count, len(df)))]


def _enqueue(store: Store, source_id: str, run_id: int, kind: str, **fields) -> int | None:
    if store.open_pending_for(source_id):
        return None
    return store.add_pending(source_id, kind=kind, run_id=run_id, **fields)

def _commit(store: Store, run_id: int, source_id: str, trunk: dict, deck_name: str,
            converted_path: Path, effective_date: str | None, rows: int, changes: int,
            *, portal, notify, reuse: bool = False) -> dict:
    """create-or-reuse -> upload -> (assign -> verify). Orphan cleanup only for decks we created."""
    work_dir = Path(converted_path).parent
    final_path = work_dir / f"{deck_name}.csv"
    if Path(converted_path) != final_path:
        shutil.copyfile(converted_path, final_path)

    deck_id = None
    created = False
    status = "upload_failed"
    try:
        if reuse:
            deck_id = portal.existing_deck_id(trunk["carrier_id"], trunk["trunk_id"])
            if deck_id is None:
                raise RuntimeError(f"no existing deck assigned to trunk '{trunk['trunk_name']}'")
        else:
            deck_id = portal.create_rate_deck(deck_name, description=trunk["trunk_name"],
                                              lrn_billing=True)
            created = True
        upload_result = portal.upload_rate_deck(deck_id, final_path,
                                                effective_date=effective_date or None, replace=True)
        if not upload_result.get("ok"):
            status = "upload_failed"
        elif reuse:
            status = "uploaded"                            # already assigned to the trunk
        else:
            assigned = portal.assign_deck_to_trunk(trunk["carrier_id"], trunk["trunk_id"], deck_id,
                                                   expect_name=deck_name)
            status = "uploaded" if assigned.get("ok") else "needs_human"
    except Exception:
        log.exception("commit failed for deck %s", deck_id)
        status = "upload_failed"
    if created and status != "uploaded" and deck_id is not None:
        try:
            portal.delete_rate_deck(deck_id)               # orphan cleanup (only decks we created)
        except Exception:
            log.exception("orphan cleanup failed for deck %s", deck_id)

    effective = _display_date(effective_date) or _dt.date.today().strftime(config.DECK_NAME_DATE_FORMAT)
    summary = f"{trunk['trunk_name']} {effective}: {rows} rows, {changes} changes"
    if status == "uploaded":
        summary += f" -> deck {deck_id}" + (" (reused)" if reuse else "")
    elif status == "needs_human":
        summary += " -> upload/assign failed, deck removed"
    if status == "uploaded":
        store.record_deck(trunk["trunk_id"], effective_date, rows, changes, str(final_path),
                          deck_id=deck_id)
    store.finish_run(run_id, trunk_id=trunk["trunk_id"], status=status, notes=summary)
    label = {"uploaded": "UPLOADED", "upload_failed": "ERROR"}.get(status, "NEEDS HUMAN")
    notify(f"{label}: {summary}")
    _audit({"source_id": source_id, "run_id": run_id, "trunk_id": trunk["trunk_id"],
            "trunk_name": trunk["trunk_name"], "status": status, "rows": rows, "deck_id": deck_id,
            "changes": changes, "converted_path": str(final_path)})
    return {"status": status, "run_id": run_id, "trunk_id": trunk["trunk_id"], "deck_name": deck_name,
            "deck_id": deck_id, "rows": rows, "changes": changes, "converted_path": str(final_path)}

def _process_single(store: Store, source_id: str, facts: EmailFacts, path: Path,
                    work_dir: Path, *, trunk_override: str | None = None, portal=None,
                    commit: bool = False, force: bool = False, notify=None,
                    today: _dt.date | None = None,
                    require_clean_warnings: bool = False, reuse: bool = False) -> dict:
    """Convert + validate + (dry-run | queue | commit) exactly one deck file."""
    notify = notify or _log_notify
    store.add_message(source_id, sender=facts.sender_domain, subject=facts.deck_filename or "")
    if commit and not force and store.source_uploaded(source_id):
        return {"status": "duplicate", "source_id": source_id}
    run_id = store.start_run(source_id)

    trunks = store.all_trunks()
    trunk_id = None
    candidates: list[str] = []
    reason_text = ""
    if trunk_override:
        forced = find_trunk(store, trunk_override)
        if forced:
            trunk_id = int(forced["trunk_id"])
        else:
            reason_text = f"trunk '{trunk_override}' not found"
    else:
        sender_carrier = (carrier_from_sender(facts.sender_domain, store)
                          if facts.sender_domain else None)
        match = match_trunks(facts.to_hints(), trunks, sender_carrier=sender_carrier)
        if not match.needs_human and match.trunk_id is not None:
            trunk_id = match.trunk_id
        else:
            candidates = [c.trunk_name for c in match.candidates if c.score > 0][:8]
            reason_text = match.reason

    if (trunk_id is not None and facts.sender_domain
            and store.carrier_for_domain(facts.sender_domain) is None):
        carrier_id = (store.get_trunk(trunk_id) or {}).get("carrier_id")
        if carrier_id:
            store.set_sender_domain(facts.sender_domain, int(carrier_id))  # auto-learn

    config_for_trunk = (store.get_config(trunk_id) or {}) if trunk_id else {}
    raw = read_clean(path)
    if trunk_id is not None:
        missing = missing_required_columns(raw.columns, config_for_trunk.get("header_aliases"))
        if missing:
            suggestion = propose_mapping(list(raw.columns), _sample_rows(raw))
            reasons = [f"missing: {missing}", f"headers: {list(raw.columns)}"]
            if suggestion:
                covered = resolve_columns(list(raw.columns), suggestion)
                leftover = [name for name in LAYOUT_REQUIRED if name not in covered]
                reasons.append("suggested_aliases: " + json.dumps(suggestion))
                reasons.append("suggestion covers all required columns" if not leftover
                               else f"suggestion still missing: {leftover}")
            if commit:
                _enqueue(store, source_id, run_id, "mapping", trunk_id=trunk_id, work_dir=work_dir,
                         converted_path=path, effective_date=facts.effective_date, rows=len(raw),
                         changes=0, reasons=reasons)
            summary = f"{path.name}: unmapped columns {missing}"
            if suggestion:
                summary += f" (LLM suggests {len(suggestion)} alias(es))"
            store.finish_run(run_id, trunk_id=trunk_id, status="needs_mapping", notes=summary)
            notify(f"NEEDS HUMAN - {summary}")
            return {"status": "needs_mapping", "run_id": run_id, "source_id": source_id,
                    "missing": missing, "suggested_aliases": suggestion}
    df = to_template(raw, config_for_trunk)
    previous = None
    if trunk_id:
        previous_row = store.last_deck(trunk_id)
        if previous_row and Path(previous_row["converted_path"]).exists():
            previous = read_clean(previous_row["converted_path"])
    issues = validate(df, previous=previous)
    errors = [issue for issue in issues if issue.level == "error"]
    warnings = [str(issue) for issue in issues if issue.level == "warn"]
    file_dates = sorted({str(value).strip() for value in df["effective_date"] if str(value).strip()})
    if facts.effective_date and file_dates and facts.effective_date not in file_dates:
        warnings.append(f"effective_date mismatch: email {facts.effective_date} vs file {file_dates[:3]}")
    if errors:
        summary = f"{path.name}: validation failed ({len(errors)} errors)"
        store.finish_run(run_id, trunk_id=trunk_id, status="validation_error", notes=summary)
        notify(f"ERROR - {summary}")
        return {"status": "validation_error", "run_id": run_id, "source_id": source_id,
                "errors": [str(issue) for issue in errors]}

    if trunk_id and not facts.effective_date:
        summary = f"{path.name}: no effective date - set it before upload"
        store.finish_run(run_id, trunk_id=trunk_id, status="needs_date", notes=summary)
        notify(f"NEEDS HUMAN - {summary}")
        return {"status": "needs_date", "run_id": run_id, "source_id": source_id}

    changes = count_changes(previous, df)
    rows = len(df)
    if trunk_id:
        trunk = store.get_trunk(trunk_id)
        deck_name = build_deck_name(trunk["trunk_name"], facts.effective_date)
        converted_path = work_dir / f"{deck_name}.csv"
    else:
        trunk = None
        deck_name = ""
        converted_path = work_dir / f"converted_{path.stem}.csv"
    df.to_csv(converted_path, index=False, encoding="utf-8")

    if trunk_id is None:
        if commit:
            _enqueue(store, source_id, run_id, "trunk", work_dir=work_dir,
                     converted_path=converted_path, effective_date=facts.effective_date,
                     rows=rows, changes=changes, reasons=[reason_text] + candidates)
        summary = f"{path.name}: trunk not resolved"
        store.finish_run(run_id, status="needs_human", notes=summary)
        notify(f"NEEDS HUMAN - {summary}")
        return {"status": "needs_human", "run_id": run_id, "source_id": source_id}

    allowed, reason = commit_allowed(store, trunk)
    if commit and not allowed:
        _enqueue(store, source_id, run_id, "approval", trunk_id=trunk_id, work_dir=work_dir,
                 converted_path=converted_path, effective_date=facts.effective_date,
                 rows=rows, changes=changes, reasons=[reason])
        summary = f"{trunk['trunk_name']}: first upload needs approval"
        store.finish_run(run_id, trunk_id=trunk_id, status="needs_human", notes=summary)
        notify(f"NEEDS HUMAN - {summary}")
        return {"status": "needs_human", "run_id": run_id, "source_id": source_id,
                "deck_name": deck_name}

    if commit and require_clean_warnings and warnings:
        _enqueue(store, source_id, run_id, "warnings", trunk_id=trunk_id, work_dir=work_dir,
                 converted_path=converted_path, effective_date=facts.effective_date,
                 rows=rows, changes=changes, reasons=warnings)
        summary = f"{trunk['trunk_name']}: {len(warnings)} warning(s) need approval"
        store.finish_run(run_id, trunk_id=trunk_id, status="needs_human", notes=summary)
        notify(f"NEEDS HUMAN - {summary}")
        return {"status": "needs_human", "run_id": run_id, "source_id": source_id,
                "deck_name": deck_name}

    if not commit:
        effective = (_display_date(facts.effective_date)
                     or (today or _dt.date.today()).strftime(config.DECK_NAME_DATE_FORMAT))
        summary = f"{trunk['trunk_name']} {effective}: {rows} rows, {changes} changes"
        store.finish_run(run_id, trunk_id=trunk_id, status="dry_run", notes=summary)
        notify(f"DRY RUN: {summary}")
        _audit({"source_id": source_id, "run_id": run_id, "trunk_id": trunk_id,
                "trunk_name": trunk["trunk_name"], "status": "dry_run", "rows": rows,
                "deck_id": None, "changes": changes, "converted_path": str(converted_path)})
        return {"status": "dry_run", "run_id": run_id, "trunk_id": trunk_id, "deck_name": deck_name,
                "rows": rows, "changes": changes, "converted_path": str(converted_path)}

    return _commit(store, run_id, source_id, trunk, deck_name, converted_path,
                   facts.effective_date, rows, changes, portal=portal, notify=notify, reuse=reuse)

def _process_files(store: Store, source_id: str, facts: EmailFacts, files,
                   work_dir: Path, *, trunk_override: str | None = None, portal=None,
                   commit: bool = False, force: bool = False, notify=None,
                   today: _dt.date | None = None,
                   require_clean_warnings: bool = False, reuse: bool = False) -> dict:
    """Process one deck, or every deck in a multi-deck input (per-file hints + idempotency)."""
    notify = notify or _log_notify
    decks = _deck_files(files)
    if not decks:
        store.add_message(source_id, sender=facts.sender_domain, subject=facts.deck_filename or "")
        run_id = store.start_run(source_id)
        summary = f"{facts.sender_domain or source_id}: no deck file found"
        store.finish_run(run_id, status="no_deck", notes=summary)
        log.info("%s", summary)                         # a mail with no deck is normal, not a human task
        return {"status": "no_deck", "run_id": run_id}
    if len(decks) == 1:
        return _process_single(store, source_id, facts, decks[0], work_dir,
                               trunk_override=trunk_override, portal=portal, commit=commit,
                               force=force, notify=notify, today=today,
                               require_clean_warnings=require_clean_warnings, reuse=reuse)

    results = []
    for deck in decks:
        results.append(_process_single(
            store, f"{source_id}#{deck.name}", _facts_for_file(facts, deck), deck, work_dir,
            trunk_override=trunk_override, portal=portal, commit=commit, force=force,
            notify=notify, today=today, require_clean_warnings=require_clean_warnings, reuse=reuse))
    notify(f"multi-deck input: {len(decks)} deck(s) processed")
    return {"status": "multi", "source_id": source_id, "results": results}

def process_email(store: Store, raw: bytes, *, portal=None, commit: bool = False,
                  use_llm: bool | None = None, input_fn=input, notify=None,
                  today: _dt.date | None = None, force: bool = False,
                  require_clean_warnings: bool = False, reuse: bool = False) -> dict:
    """Process one raw email (may carry multiple decks)."""
    notify = notify or _log_notify
    parsed = parse_email(raw)
    source_id = parsed.message_id or ("sha1:" + hashlib.sha1(raw).hexdigest())
    work_dir = archive_dir(source_id, today)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "message.eml").write_bytes(raw)
    facts = extract_facts(parsed, use_llm=use_llm)
    files = fetch_deck_files(parsed, work_dir)
    return _process_files(store, source_id, facts, files, work_dir, portal=portal,
                          commit=commit, force=force, notify=notify, today=today,
                          require_clean_warnings=require_clean_warnings, reuse=reuse)

def process_item(store: Store, item: InputItem, *, portal=None, commit: bool = False,
                 input_fn=input, notify=None, today: _dt.date | None = None,
                 force: bool = False, require_clean_warnings: bool = False,
                 reuse: bool = False) -> dict:
    """Process one already-materialized input (email or directory)."""
    notify = notify or _log_notify
    return _process_files(store, item.source_id, item.facts, item.deck_files, item.work_dir,
                          trunk_override=item.meta.get("trunk_override"), portal=portal,
                          commit=commit, force=force, notify=notify, today=today,
                          require_clean_warnings=require_clean_warnings, reuse=reuse)

def process_pending(store: Store, *, portal, commit: bool, notify=None,
                    today: _dt.date | None = None) -> list[dict]:
    """Commit items a human has resolved; approval items get one more gate."""
    notify = notify or _log_notify
    if not commit:
        return []
    results = []
    for item in store.list_pending("resolved"):
        if item.get("kind") == "mapping":
            store.set_pending_status(item["id"], "applied")   # aliases now set; a re-poll converts
            continue
        trunk_id = item.get("resolved_trunk_id") or item.get("trunk_id")
        trunk = store.get_trunk(trunk_id) if trunk_id else None
        if trunk is None:
            store.set_pending_status(item["id"], "open")
            notify(f"NEEDS HUMAN - pending #{item['id']}: trunk still unresolved")
            continue
        allowed, reason = commit_allowed(store, trunk)
        if not allowed:
            if item.get("approved"):
                store.approve(int(trunk["trunk_id"]), by=item.get("resolved_by") or "human",
                              carrier_id=trunk.get("carrier_id"))
                allowed = True
            else:
                store.reopen_pending(item["id"], kind="approval", trunk_id=int(trunk["trunk_id"]),
                                     reasons=[reason])
                notify(f"NEEDS HUMAN - pending #{item['id']}: {trunk['trunk_name']} needs approval")
                continue
        run_id = item.get("run_id") or store.start_run(item["source_id"])
        deck_name = build_deck_name(trunk["trunk_name"], item.get("effective_date"))
        if not deck_name:
            notify(f"NEEDS HUMAN - pending #{item['id']}: no effective date")
            continue
        result = _commit(store, run_id, item["source_id"], trunk, deck_name,
                         Path(item["converted_path"]), item.get("effective_date"),
                         item.get("rows") or 0, item.get("changes") or 0, portal=portal, notify=notify)
        store.set_pending_status(item["id"], "committed" if result["status"] == "uploaded" else "failed")
        results.append(result)
    return results

def run_once(store: Store, source, *, portal=None, commit: bool = False,
             today: _dt.date | None = None, force: bool = False,
             require_clean_warnings: bool = False, reuse: bool = False,
             limit: int | None = None, **kwargs) -> list[dict]:
    today = today or _dt.date.today()
    work_root = config.ARCHIVE_DIR / today.isoformat()
    results = []
    for item in source.fetch(work_root, store=(None if force else store), limit=limit):
        results.append(process_item(store, item, portal=portal, commit=commit, today=today,
                                    force=force, require_clean_warnings=require_clean_warnings,
                                    reuse=reuse, **kwargs))
    return results

def _run_source(store: Store, args, portal) -> list[dict]:
    today = _dt.date.today()
    if args.source == "directory":
        if not args.dir:
            raise SystemExit("--source directory needs --dir <folder>")
        source = DirectorySource(args.dir, trunk_override=args.trunk)
        results = run_once(store, source, portal=portal, commit=args.commit, today=today,
                           force=args.force, require_clean_warnings=args.require_clean_warnings,
                           reuse=args.reuse_deck, limit=args.limit)
    else:
        from mailbox import Mailbox
        with Mailbox() as mailbox:
            source = EmailSource(mailbox)
            results = run_once(store, source, portal=portal, commit=args.commit, today=today,
                               force=args.force, require_clean_warnings=args.require_clean_warnings,
                               reuse=args.reuse_deck, limit=args.limit)
    results += process_pending(store, portal=portal, commit=args.commit, today=today)
    return results

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dummy orchestrator: process rate-deck inputs end to end.")
    parser.add_argument("--source", choices=("inbox", "directory"), default="inbox")
    parser.add_argument("--dir", help="folder of deck files (with --source directory)")
    parser.add_argument("--trunk", help="force a trunk name (directory mode)")
    parser.add_argument("--eml", help="process a single .eml file (dev)")
    parser.add_argument("--loop", action="store_true", help="poll continuously")
    parser.add_argument("--interval", type=int, default=config.ORCH_INTERVAL, help="seconds between polls")
    parser.add_argument("--commit", action="store_true", help="actually create decks and upload (default: dry-run)")
    parser.add_argument("--force", action="store_true", help="reprocess even if already uploaded")
    parser.add_argument("--reuse-deck", action="store_true",
                        help="upload into each trunk's existing deck instead of creating a new one")
    parser.add_argument("--limit", type=int,
                        help="process at most N input(s) this run (newest first for the inbox)")
    parser.add_argument("--require-clean-warnings", action="store_true",
                        help="hold uploads for human approval when validator warnings exist")
    parser.add_argument("--headed", action="store_true", help="show the browser on --commit")
    parser.add_argument("--db", default=str(config.DB_PATH))
    args = parser.parse_args(argv)
    logging_setup.configure()

    with Store.open(args.db) as store:
        if args.commit:
            from portal import Portal
            portal_ctx = Portal(headless=not args.headed)
        else:
            portal_ctx = contextlib.nullcontext(None)

        with portal_ctx as portal:
            if args.eml:
                data = Path(args.eml).read_bytes()
                head = data[:2000].lower()
                if b"subject:" not in head and b"from:" not in head:
                    print("error: --eml expects a raw email (.eml); for a deck CSV use "
                          "--source directory --dir <folder>", file=sys.stderr)
                    return 2
                results = [process_email(store, data, portal=portal, commit=args.commit,
                                         force=args.force,
                                         require_clean_warnings=args.require_clean_warnings,
                                         reuse=args.reuse_deck)]
                print(json.dumps(results, default=str, indent=2))
                return 0
            if args.loop:
                while True:
                    try:
                        results = _run_source(store, args, portal)
                        if results:
                            log.info("processed %d item(s)", len(results))
                    except Exception:
                        log.exception("poll failed")
                    time.sleep(args.interval)
            print(f"(source={args.source}) processing once; commit={args.commit}")
            results = _run_source(store, args, portal)
            print(f"processed {len(results)} item(s)")
            return 0

if __name__ == "__main__":
    raise SystemExit(main())
