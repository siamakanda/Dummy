import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from mapper import parse_date_from_filename
from store import Store
from upload import (build_deck_name, build_plan, commit_allowed, execute,
                    load_effective_date, print_plan)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

HEADER = ("prefix,destination,inter_rate,intra_rate,indeterminate_rate,connect_fee,"
          "init_duration,increment,grace_time,effective_date")


class FakePortal:
    def __init__(self, existing=777):
        self.created = []
        self.uploaded = []
        self.existing = existing

    def create_rate_deck(self, name, **kwargs):
        self.created.append((name, kwargs))
        return 999

    def upload_rate_deck(self, deck_id, path, **kwargs):
        self.uploaded.append((deck_id, path, kwargs))
        return {"ok": True, "deck_id": deck_id, "screenshot": "shot.png"}

    def existing_deck_id(self, carrier_id, trunk_id):
        return self.existing


def write_deck(path, effective="2026-11-01", rows=2):
    lines = [HEADER]
    for i in range(rows):
        lines.append(f"120120{i},NANPA,0.001,0.002,0.002,0,12,6,,{effective}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_store(tmp_path):
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    return store


def test_build_deck_name_with_date():
    assert build_deck_name("DW USA CONVO TRUNK 6060", "2026-11-01") == "DW USA CONVO TRUNK 6060 11_01_2026"


def test_build_deck_name_without_date():
    assert build_deck_name("BTCL TNT TRUNK", None) is None
    assert build_deck_name("BTCL TNT TRUNK", "") is None


def test_parse_date_from_filename():
    assert parse_date_from_filename("InstaCall_ICRA_23599_E20261113_US.csv") == "2026-11-13"
    assert parse_date_from_filename("deck_20261113.csv") == "2026-11-13"
    assert parse_date_from_filename("deck_2026-11-13.csv") == "2026-11-13"
    assert parse_date_from_filename("deck_11_13_2026.csv") == "2026-11-13"
    assert parse_date_from_filename("deck.csv") == ""


def test_build_plan_requires_date(tmp_path):
    store = make_store(tmp_path)
    deck = write_deck(tmp_path / "deck.csv", effective="")
    try:
        build_plan(store, "BTCL TNT TRUNK", deck)
    except ValueError as exc:
        assert "effective date required" in str(exc)
    else:
        raise AssertionError("expected ValueError when no effective date")


def test_build_plan_unknown_trunk_is_a_valueerror(tmp_path):
    store = make_store(tmp_path)
    deck = write_deck(tmp_path / "deck.csv", effective="2026-11-01")
    try:
        build_plan(store, "NO SUCH TRUNK", deck)
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown trunk")


def test_load_effective_date(tmp_path):
    deck = write_deck(tmp_path / "deck.csv", effective="2026-11-02")
    assert load_effective_date(deck) == "2026-11-02"
    blank = write_deck(tmp_path / "blank.csv", effective="")
    assert load_effective_date(blank) is None


def test_commit_gate(tmp_path):
    store = make_store(tmp_path)
    dev = store.get_trunk(87)                       # BTCL TNT TRUNK
    assert dev["trunk_name"] == "BTCL TNT TRUNK"
    assert commit_allowed(store, dev)[0] is True
    carrier = store.get_trunk(82)                   # DW USA CONVO TRUNK 6060
    assert commit_allowed(store, carrier)[0] is False
    store.approve(82, by="tester")
    assert commit_allowed(store, store.get_trunk(82))[0] is True


def test_build_plan(tmp_path):
    store = make_store(tmp_path)
    deck = write_deck(tmp_path / "deck.csv", effective="2026-11-01", rows=2)
    plan = build_plan(store, "BTCL TNT TRUNK", deck)
    assert plan.trunk_id == 87
    assert plan.deck_name == "BTCL TNT TRUNK 11_01_2026"
    assert plan.lrn_billing is True and plan.replace is True
    assert plan.rows == 2


def test_execute_dry_run_then_commit(tmp_path):
    store = make_store(tmp_path)
    deck = write_deck(tmp_path / "deck.csv", effective="2026-11-01")
    plan = build_plan(store, "BTCL TNT TRUNK", deck)
    portal = FakePortal()

    assert execute(plan, portal, commit=False) == {"committed": False}
    assert not portal.created and not portal.uploaded

    result = execute(plan, portal, commit=True)
    assert result["deck_id"] == 999
    assert portal.created[0][0] == plan.deck_name
    assert portal.created[0][1]["lrn_billing"] is True
    assert portal.uploaded[0][0] == 999
    assert portal.uploaded[0][2]["replace"] is True


def test_execute_reuse_deck(tmp_path):
    store = make_store(tmp_path)
    deck = write_deck(tmp_path / "deck.csv", effective="2026-11-01")
    plan = build_plan(store, "BTCL TNT TRUNK", deck)
    portal = FakePortal(existing=777)

    result = execute(plan, portal, commit=True, reuse=True)

    assert result["deck_id"] == 777
    assert not portal.created                    # reused, not created
    assert portal.uploaded[0][0] == 777


def test_execute_reuse_deck_missing(tmp_path):
    store = make_store(tmp_path)
    deck = write_deck(tmp_path / "deck.csv", effective="2026-11-01")
    plan = build_plan(store, "BTCL TNT TRUNK", deck)
    portal = FakePortal(existing=None)

    result = execute(plan, portal, commit=True, reuse=True)

    assert result["committed"] is False and result["ok"] is False
    assert not portal.created and not portal.uploaded


def test_print_plan(tmp_path):
    store = make_store(tmp_path)
    deck = write_deck(tmp_path / "deck.csv", effective="2026-11-01")
    plan = build_plan(store, "BTCL TNT TRUNK", deck)
    lines = []
    print_plan(plan, out=lines.append)
    text = "\n".join(lines)
    assert "BTCL TNT TRUNK 11_01_2026" in text
    assert "Use LRN for billing" in text
    assert str(deck) in text
