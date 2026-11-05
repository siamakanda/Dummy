"""Housekeeping: delete rate decks older than N months (M15). Dry-run by default.

Safety: never deletes a deck **assigned to a trunk**, never deletes a deck whose name has no parsable
date, and never deletes anything without ``--commit``.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import re

import config
from portal import Portal
from store import Store

DATE_FORMATS = ("%m_%d_%Y", "%m/%d/%Y", "%m_%d_%y", "%m/%d/%y", "%Y-%m-%d", "%Y_%m_%d")
DATE_RE = re.compile(r"(\d{1,4}[/_-]\d{1,2}[/_-]\d{2,4})")


def parse_deck_date(name: str) -> dt.date | None:
    match = DATE_RE.search(name or "")
    if not match:
        return None
    token = match.group(1)
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    return None


def months_ago(today: dt.date, months: int) -> dt.date:
    total = today.year * 12 + (today.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def plan_cleanup(decks, *, older_than_months: int = 4, today: dt.date | None = None,
                 is_assigned=None):
    """Return ``(to_delete, skipped[(deck, reason)], cutoff)``."""
    today = today or dt.date.today()
    cutoff = months_ago(today, older_than_months)
    to_delete, skipped = [], []
    for deck in decks:
        date = parse_deck_date(deck.get("name", ""))
        if date is None:
            skipped.append((deck, "no date in name"))
            continue
        if date >= cutoff:
            continue
        if is_assigned is not None and is_assigned(deck["deck_id"]):
            skipped.append((deck, "assigned to a trunk"))
            continue
        to_delete.append(deck)
    return to_delete, skipped, cutoff


def _matches_trunk_name(name: str, trunk_names) -> bool:
    for trunk in trunk_names:
        trunk = (trunk or "").strip()
        if trunk and name.startswith(trunk) and parse_deck_date(name[len(trunk):]) is not None:
            return True
    return False


def plan_orphans(decks, trunk_names, *, min_age_days: int = 1, today: dt.date | None = None,
                 is_assigned=None):
    """Decks named ``<managed trunk> <date>`` that are **unassigned** and not brand new.

    ``min_age_days`` keeps us from racing a run that created a deck moments ago.
    """
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=min_age_days)
    orphans = []
    for deck in decks:
        name = (deck.get("name") or "").strip()
        date = parse_deck_date(name)
        if date is None or not _matches_trunk_name(name, trunk_names):
            continue
        if date > cutoff:
            continue
        if is_assigned is not None and is_assigned(deck["deck_id"]):
            continue
        orphans.append(deck)
    return orphans, cutoff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete old / orphan rate decks (dry-run by default).")
    parser.add_argument("--older-than-months", type=int, default=4,
                        help="age mode: delete decks older than N months (default 4)")
    parser.add_argument("--orphans", action="store_true",
                        help="orphan mode: sweep unassigned <trunk> <date> decks only")
    parser.add_argument("--min-age-days", type=int, default=1,
                        help="orphan mode: ignore decks newer than this many days (default 1)")
    parser.add_argument("--commit", action="store_true", help="actually delete the decks")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args(argv)

    with Store.open(config.DB_PATH) as store:
        trunk_names = [t["trunk_name"] for t in store.all_trunks()]

        with Portal(headless=not args.headed) as portal:
            decks = portal.list_decks()
            if args.orphans:
                targets, cutoff = plan_orphans(decks, trunk_names, min_age_days=args.min_age_days,
                                               is_assigned=portal.deck_is_assigned)
                print(f"{len(decks)} decks; orphan sweep (unassigned Dummy decks older than "
                      f"{args.min_age_days} day(s), cutoff {cutoff.isoformat()})")
            else:
                targets, skipped, cutoff = plan_cleanup(
                    decks, older_than_months=args.older_than_months,
                    is_assigned=portal.deck_is_assigned)
                print(f"{len(decks)} decks; cutoff {cutoff.isoformat()} "
                      f"(older than {args.older_than_months} months)")
                for deck, why in skipped:
                    print(f"  skip   #{deck['deck_id']:<5} {deck['name']}  ({why})")

            for deck in targets:
                print(f"  DELETE #{deck['deck_id']:<5} {deck['name']}")
            if not args.commit:
                print(f"\nDRY RUN: {len(targets)} deck(s) would be deleted; add --commit to delete.")
                return 0
            deleted = 0
            for deck in targets:
                ok = portal.delete_rate_deck(deck["deck_id"])
                deleted += 1 if ok else 0
                print(f"  deleted #{deck['deck_id']}: {ok}")
            print(f"\nDeleted {deleted}/{len(targets)} deck(s).")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
