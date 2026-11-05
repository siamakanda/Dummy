import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from review import choose_trunk, resolve_and_record
from store import Store
from trunk_matcher import Candidate, MatchResult, TrunkHints, match_trunks

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

TRUNKS = [
    {"trunk_id": 1, "carrier_id": 1, "carrier_name": "ACME",
     "trunk_name": "ACME GOLD", "prefix": "1234", "cost_deck": ""},
    {"trunk_id": 2, "carrier_id": 1, "carrier_name": "ACME",
     "trunk_name": "ACME SILVER", "prefix": "5678", "cost_deck": ""},
]

LOW_CONFIDENCE = MatchResult(
    trunk_id=None, trunk_name="", carrier_id=1, carrier_name="ACME",
    confidence=0.4, reason="low confidence",
    candidates=[Candidate(1, "ACME GOLD", 1, "ACME", 0.4, "name match 0.40"),
                Candidate(2, "ACME SILVER", 1, "ACME", 0.3, "name match 0.30")],
    ambiguous=False, threshold=0.85)

NO_SIGNAL = MatchResult(
    trunk_id=None, trunk_name="", carrier_id=None, carrier_name="",
    confidence=0.0, reason="no signal", candidates=[], ambiguous=False, threshold=0.85)

SILENT = lambda *a, **k: None  # noqa: E731


def test_confident_match_does_not_prompt():
    match = match_trunks(TrunkHints(tech_prefix="1234"), TRUNKS)
    assert not match.needs_human

    def boom(*args, **kwargs):
        raise AssertionError("input_fn must not be called for a confident match")

    assert choose_trunk(match, TRUNKS, input_fn=boom, output_fn=SILENT) == 1


def test_low_confidence_prompts_and_selects():
    chosen = choose_trunk(LOW_CONFIDENCE, TRUNKS, input_fn=lambda *a, **k: "1", output_fn=SILENT)
    assert chosen == 1
    chosen = choose_trunk(LOW_CONFIDENCE, TRUNKS, input_fn=lambda *a, **k: "2", output_fn=SILENT)
    assert chosen == 2


def test_skip_returns_none():
    assert choose_trunk(NO_SIGNAL, TRUNKS, input_fn=lambda *a, **k: "0", output_fn=SILENT) is None
    assert choose_trunk(NO_SIGNAL, TRUNKS, input_fn=lambda *a, **k: "1", output_fn=SILENT) is None


def test_resolve_records_decision(tmp_path):
    store = Store(tmp_path / "dummy.db")
    run_id = store.start_run("<m@x>")
    match = match_trunks(TrunkHints(tech_prefix="1234"), TRUNKS)
    chosen = resolve_and_record(store, run_id, match, TRUNKS, output_fn=SILENT)
    assert chosen == 1
    decision = store.conn.execute("SELECT * FROM decisions WHERE run_id = ?", (run_id,)).fetchone()
    assert decision is not None and decision["answer"] == "1"


def test_cli_set_domain_and_aliases(tmp_path):
    import review

    db = tmp_path / "dummy.db"
    store = Store(db)
    store.load_trunks(FIXTURES / "trunks.csv")
    store.close()

    assert review.main(["--db", str(db), "set-domain", "example.com", "58"]) == 0
    assert review.main(["--db", str(db), "set-aliases", "BTCL TNT TRUNK",
                        '{"prefix": ["mydial"]}']) == 0

    check = Store(db)
    assert check.carrier_for_domain("example.com") == 58
    assert check.get_config(87)["header_aliases"] == {"prefix": ["mydial"]}
    check.close()


def test_apply_mapping_saves_aliases_and_marks_applied(tmp_path):
    import review

    db = tmp_path / "dummy.db"
    store = Store(db)
    store.load_trunks(FIXTURES / "trunks.csv")
    run_id = store.start_run("<map@x>")
    pid = store.add_pending("<map@x>", kind="mapping", run_id=run_id, trunk_id=87,
                            reasons=["missing: ['prefix']",
                                     'suggested_aliases: {"prefix": ["Foo"]}'])
    store.close()

    assert review.main(["--db", str(db), "apply-mapping", str(pid)]) == 0

    check = Store(db)
    assert check.get_config(87)["header_aliases"] == {"prefix": ["Foo"]}
    assert check.get_pending(pid)["status"] == "applied"
    check.close()
