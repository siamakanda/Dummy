import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from cleanup import months_ago, parse_deck_date, plan_cleanup, plan_orphans


def test_parse_deck_date():
    assert parse_deck_date("BTCL TNT TRUNK 12_02_2026") == dt.date(2026, 12, 2)
    assert parse_deck_date("NUSO Platinum Plus 11_02_2026") == dt.date(2026, 11, 2)
    assert parse_deck_date("NO DATE HERE") is None
    assert parse_deck_date("") is None


def test_months_ago():
    assert months_ago(dt.date(2026, 11, 4), 4) == dt.date(2026, 7, 4)
    assert months_ago(dt.date(2026, 1, 31), 1) == dt.date(2025, 12, 31)


def test_plan_cleanup():
    decks = [
        {"deck_id": 1, "name": "OLD TRUNK 06_01_2026"},
        {"deck_id": 2, "name": "NEW TRUNK 11_01_2026"},
        {"deck_id": 3, "name": "NO DATE"},
        {"deck_id": 4, "name": "ASSIGNED OLD 05_01_2026"},
    ]
    assigned = {4}
    to_delete, skipped, cutoff = plan_cleanup(
        decks, older_than_months=4, today=dt.date(2026, 11, 4),
        is_assigned=lambda deck_id: deck_id in assigned)

    assert cutoff == dt.date(2026, 7, 4)
    assert [d["deck_id"] for d in to_delete] == [1]
    reasons = {deck["deck_id"]: why for deck, why in skipped}
    assert reasons[3] == "no date in name"
    assert reasons[4] == "assigned to a trunk"


def test_plan_orphans():
    decks = [
        {"deck_id": 1, "name": "BTCL TNT TRUNK 10_01_2026"},   # old, unassigned -> orphan
        {"deck_id": 2, "name": "BTCL TNT TRUNK 11_04_2026"},   # today -> too new
        {"deck_id": 3, "name": "BTCL TNT TRUNK 10_02_2026"},   # old but assigned -> keep
        {"deck_id": 4, "name": "Random Deck 10_01_2026"},      # not a managed trunk -> keep
        {"deck_id": 5, "name": "NO DATE"},                     # no date -> keep
    ]
    orphans, cutoff = plan_orphans(decks, ["BTCL TNT TRUNK"], min_age_days=1,
                                   today=dt.date(2026, 11, 4), is_assigned=lambda i: i == 3)

    assert cutoff == dt.date(2026, 11, 3)
    assert [d["deck_id"] for d in orphans] == [1]


def test_plan_orphans_matches_numeric_trunk_name():
    decks = [{"deck_id": 9, "name": "DW USA CONVO TRUNK 6060 10_01_2026"}]
    orphans, _ = plan_orphans(decks, ["DW USA CONVO TRUNK 6060", "BTCL TNT TRUNK"],
                              min_age_days=1, today=dt.date(2026, 11, 4),
                              is_assigned=lambda i: False)

    assert [d["deck_id"] for d in orphans] == [9]
