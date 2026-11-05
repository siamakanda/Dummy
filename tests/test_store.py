import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from store import Store

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TRUNKS = FIXTURES / "trunks.csv"


def make_store(tmp_path):
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(TRUNKS)
    return store


def test_load_trunks(tmp_path):
    store = make_store(tmp_path)
    assert len(store.all_trunks()) == 52
    assert len(store.list_carriers()) == 25
    assert len(store.all_trunks(carrier_id=58)) == 4
    trunk = store.get_trunk(82)
    assert trunk["trunk_name"] == "DW USA CONVO TRUNK 6060"
    assert trunk["prefix"] == "6060"
    assert trunk["carrier_name"] == "DW Telco"


def test_name_aliases_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.set_config(87, name_aliases=["BTCL TNT Domestic"])

    assert store.get_config(87)["name_aliases"] == ["BTCL TNT Domestic"]

    trunks = {t["trunk_id"]: t for t in store.all_trunks()}
    assert trunks[87]["name_aliases"] == ["BTCL TNT Domestic"]
    assert trunks[82]["name_aliases"] == []      # trunks with no config default to empty


def test_load_trunks_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.load_trunks(TRUNKS)
    assert len(store.all_trunks()) == 52


def test_sender_domain_roundtrip(tmp_path):
    store = make_store(tmp_path)
    assert store.carrier_for_domain("dialworld.com") is None
    store.set_sender_domain("DialWorld.com", 58)
    assert store.carrier_for_domain("dialworld.com") == 58   # case-insensitive
    store.set_sender_domain("dialworld.com", 59)
    assert store.carrier_for_domain("dialworld.com") == 59    # upsert


def test_approval(tmp_path):
    store = make_store(tmp_path)
    assert not store.is_approved(82)
    store.approve(82, by="tester", carrier_id=58)
    assert store.is_approved(82)


def test_config_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.set_config(82, carrier_id=58, tech_prefix="6060",
                     default_init_duration=12, default_increment=6,
                     header_aliases={"prefix": ["dialcode"]})
    config = store.get_config(82)
    assert config["tech_prefix"] == "6060"
    assert config["header_aliases"] == {"prefix": ["dialcode"]}
    store.set_config(82, default_increment=1)
    assert store.get_config(82)["default_increment"] == 1
    assert store.get_config(82)["tech_prefix"] == "6060"   # other fields preserved


def test_message_idempotency(tmp_path):
    store = make_store(tmp_path)
    assert store.add_message("<abc@x>", sender="a@b.com", subject="deck") is True
    assert store.add_message("<abc@x>") is False
    assert store.has_message("<abc@x>")


def test_run_and_decision(tmp_path):
    store = make_store(tmp_path)
    run_id = store.start_run("<abc@x>")
    store.add_decision(run_id, "trunk_match", "auto", "82")
    store.finish_run(run_id, trunk_id=82, status="done")
    row = store.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["trunk_id"] == 82 and row["status"] == "done"
    decision = store.conn.execute("SELECT * FROM decisions WHERE run_id = ?", (run_id,)).fetchone()
    assert decision["answer"] == "82"


def test_pending_queue(tmp_path):
    store = make_store(tmp_path)
    pending_id = store.add_pending("src1", kind="trunk", run_id=1, rows=3, reasons=["ambiguous"])
    assert store.get_pending(pending_id)["kind"] == "trunk"
    assert store.open_pending_for("src1")["id"] == pending_id

    store.resolve_pending(pending_id, trunk_id=43, approve=True, by="tester")
    item = store.get_pending(pending_id)
    assert item["status"] == "resolved"
    assert item["resolved_trunk_id"] == 43 and item["approved"] == 1

    store.reopen_pending(pending_id, kind="approval", trunk_id=43, reasons=["needs approval"])
    item = store.get_pending(pending_id)
    assert item["status"] == "open" and item["kind"] == "approval"

    store.set_pending_status(pending_id, "committed")
    assert store.list_pending("open") == []
