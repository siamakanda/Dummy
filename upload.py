"""Create a rate deck on the switch portal and upload a converted file (AGENTS.md sections 2 and 6).

DRY-RUN by default: it only prints the plan. Add ``--commit`` to act, and only on the development
trunk (``BTCL TNT TRUNK``) or a trunk already approved in the store.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass

import config
from cleaner import read_clean
from mapper import DATE_FORMATS
from store import Store

@dataclass
class Plan:
    trunk_id: int
    trunk_name: str
    carrier_id: int | None
    carrier_name: str
    deck_name: str
    effective_date: str | None
    csv_path: str
    rows: int
    lrn_billing: bool = True
    replace: bool = True

def load_effective_date(csv_path) -> str | None:
    """The first non-empty effective_date in a converted deck (ISO), or None."""
    frame = read_clean(csv_path)
    if "effective_date" not in frame.columns:
        return None
    for value in frame["effective_date"]:
        text = str(value).strip()
        if text:
            return text
    return None

def build_deck_name(trunk_name: str, effective_date: str | None,
                    today: dt.date | None = None) -> str | None:
    """`<trunk name> MM_DD_YYYY` from the effective date; ``None`` when there is no usable date.

    Never invents a date: a missing/unparseable date must be set by a human (``today`` is kept for
    signature compatibility only and is ignored).
    """
    if not effective_date:
        return None
    text = str(effective_date).strip()
    day = None
    for fmt in DATE_FORMATS:
        try:
            day = dt.datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    if day is None:
        return None
    return f"{trunk_name} {day.strftime(config.DECK_NAME_DATE_FORMAT)}"

def find_trunk(store: Store, trunk_name: str) -> dict | None:
    wanted = trunk_name.strip().lower()
    for trunk in store.all_trunks():
        if str(trunk["trunk_name"]).strip().lower() == wanted:
            return trunk
    return None

def commit_allowed(store: Store, trunk: dict) -> tuple[bool, str]:
    """Writes are allowed only for the dev trunk or a trunk already human-approved."""
    if str(trunk["trunk_name"]).strip().lower() == config.DEV_TRUNK_NAME.strip().lower():
        return True, f"development trunk ({config.DEV_TRUNK_NAME})"
    if store.is_approved(int(trunk["trunk_id"])):
        return True, "trunk already approved"
    return False, (f"trunk '{trunk['trunk_name']}' is not approved and not the development trunk "
                   f"({config.DEV_TRUNK_NAME}); refusing to write")

def build_plan(store: Store, trunk_name: str, csv_path, *, today: dt.date | None = None,
               effective_date: str | None = None) -> Plan:
    trunk = find_trunk(store, trunk_name)
    if trunk is None:
        raise ValueError(f"trunk '{trunk_name}' not found in the store (run scrape_trunks.py / load_trunks first)")
    frame = read_clean(csv_path)
    effective = effective_date
    if not effective and "effective_date" in frame.columns:
        for value in frame["effective_date"]:
            text = str(value).strip()
            if text:
                effective = text
                break
    deck_name = build_deck_name(str(trunk["trunk_name"]), effective)
    if deck_name is None:
        raise ValueError(f"effective date required for trunk '{trunk['trunk_name']}' (got {effective!r})")
    return Plan(
        trunk_id=int(trunk["trunk_id"]),
        trunk_name=str(trunk["trunk_name"]),
        carrier_id=trunk.get("carrier_id"),
        carrier_name=str(trunk.get("carrier_name") or ""),
        deck_name=deck_name,
        effective_date=effective,
        csv_path=str(csv_path),
        rows=len(frame),
    )

def print_plan(plan: Plan, out=print) -> None:
    out("DRY RUN - nothing is written (add --commit to perform it)")
    out(f"  trunk       : {plan.trunk_name} (id {plan.trunk_id}, carrier {plan.carrier_name})")
    out(f"  deck name   : {plan.deck_name}")
    out(f"  use LRN     : {'yes' if plan.lrn_billing else 'no'}   (Use LRN for billing)")
    out(f"  type / mode : carrier / npanxx")
    out(f"  effective   : {plan.effective_date or '(none)'}")
    out(f"  replace     : {'yes' if plan.replace else 'no'}")
    out(f"  file        : {plan.csv_path} ({plan.rows} rows)")

def execute(plan: Plan, portal, *, commit: bool, reuse: bool = False) -> dict:
    """Create the deck (or reuse the trunk's) and upload, or do nothing in dry-run."""
    if not commit:
        return {"committed": False}
    if reuse:
        deck_id = portal.existing_deck_id(plan.carrier_id, plan.trunk_id)
        if deck_id is None:
            return {"committed": False, "ok": False,
                    "error": f"no existing deck is assigned to trunk '{plan.trunk_name}'"}
    else:
        deck_id = portal.create_rate_deck(plan.deck_name, description=plan.trunk_name,
                                          lrn_billing=plan.lrn_billing)
    result = portal.upload_rate_deck(deck_id, plan.csv_path,
                                     effective_date=plan.effective_date, replace=plan.replace)
    return {"committed": True, "deck_id": deck_id, **result}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a portal rate deck and upload a converted file.")
    parser.add_argument("csv_path", help="converted NPANXX deck")
    parser.add_argument("--trunk", required=True, help="target trunk name (e.g. \"BTCL TNT TRUNK\")")
    parser.add_argument("--deck-id", type=int, help="upload to an existing deck instead of creating one")
    parser.add_argument("--effective-date", help="override the effective date (YYYY-MM-DD)")
    parser.add_argument("--no-replace", action="store_true", help="do not tick 'replace all existing rates'")
    parser.add_argument("--reuse-deck", action="store_true",
                        help="upload into the trunk's existing deck instead of creating a new one")
    parser.add_argument("--preview", action="store_true", help="open the browser and screenshot, no writes")
    parser.add_argument("--commit", action="store_true", help="actually create the deck and upload")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--db", default=str(config.DB_PATH))
    args = parser.parse_args(argv)

    with Store.open(args.db) as store:
        try:
            plan = build_plan(store, args.trunk, args.csv_path)
        except ValueError as exc:
            print(f"\n{exc}\nSet the effective date with --effective-date YYYY-MM-DD.", file=sys.stderr)
            return 2
        if args.effective_date:
            name = build_deck_name(plan.trunk_name, args.effective_date)
            if name is None:
                print(f"\ninvalid --effective-date {args.effective_date!r} (use YYYY-MM-DD)", file=sys.stderr)
                return 2
            plan.effective_date = args.effective_date
            plan.deck_name = name
        if args.no_replace:
            plan.replace = False
        print_plan(plan)
        if args.reuse_deck:
            print("  reuse       : yes (upload into the trunk's existing deck; no new deck)")

        if args.commit:
            allowed, reason = commit_allowed(store, store.get_trunk(plan.trunk_id))
            if not allowed:
                print(f"\nREFUSED: {reason}", file=sys.stderr)
                return 2

        if not (args.commit or args.preview):
            print("\n(dry run) no changes made.")
            return 0

        from portal import Portal      # imported here so dry-run never needs Playwright
        with Portal(headless=not args.headed) as portal:
            if args.preview:
                portal.goto("rate_decks.php")
                portal.snapshot("preview_rate_decks")
                if args.deck_id:
                    portal.goto(f"rate_deck_upload.php?id={args.deck_id}")
                    portal.snapshot(f"preview_upload_{args.deck_id}")
                print(f"\nPreview screenshots written to {config.SNAP_DIR} (no changes made).")
                return 0

            if args.deck_id:
                result = portal.upload_rate_deck(args.deck_id, plan.csv_path,
                                                 effective_date=plan.effective_date, replace=plan.replace)
                print(f"\nUploaded to existing deck {args.deck_id}: ok={result.get('ok')} "
                      f"({result.get('screenshot')})")
                return 0 if result.get("ok") else 1

            result = execute(plan, portal, commit=True, reuse=args.reuse_deck)
            if result.get("error"):
                print(f"\nNot committed: {result['error']}")
                return 1
            print(f"\nCommitted: deck {result.get('deck_id')} ok={result.get('ok')}")
            print(f"  screenshot: {result.get('screenshot')}")
            return 0 if result.get("ok") else 1

if __name__ == "__main__":
    raise SystemExit(main())
