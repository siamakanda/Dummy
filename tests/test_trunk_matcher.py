import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from store import Store
from trunk_matcher import TrunkHints, match_trunks

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def all_trunks(tmp_path):
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    return store.all_trunks()


def test_dial_world_prefix_6060(tmp_path):
    result = match_trunks(TrunkHints(tech_prefix="6060"), all_trunks(tmp_path))
    assert result.trunk_name == "DW USA CONVO TRUNK 6060"
    assert result.confidence == 1.0
    assert not result.needs_human


def test_ani_no_trunk_asks_human(tmp_path):
    result = match_trunks(TrunkHints(sender="noreply@ani.net", sender_domain="ani.net"),
                          all_trunks(tmp_path), sender_carrier=42)
    assert result.trunk_id is None
    assert result.needs_human


def test_unknown_prefix_asks_human(tmp_path):
    result = match_trunks(TrunkHints(tech_prefix="9999"), all_trunks(tmp_path))
    assert result.needs_human


def test_unrelated_name_asks_human(tmp_path):
    result = match_trunks(TrunkHints(trunk_name="totally unrelated product"),
                          all_trunks(tmp_path))
    assert result.needs_human


def test_declared_alias_matches(tmp_path):
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    store.set_config(87, name_aliases=["BTCL TNT Domestic"])

    result = match_trunks(TrunkHints(deck_name="BTCL TNT Domestic"), store.all_trunks())

    assert result.trunk_id == 87
    assert result.confidence == 1.0
    assert not result.needs_human


def test_alias_matches_deck_name_when_product_differs(tmp_path):
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    store.set_config(87, name_aliases=["BTCL TNT Domestic"])

    # the LLM sets product="TNT Domestic"; the alias lives on the deck filename
    result = match_trunks(TrunkHints(product="TNT Domestic", deck_name="BTCL TNT Domestic"),
                          store.all_trunks())

    assert result.trunk_id == 87
    assert not result.needs_human


def test_fuzzy_name(tmp_path):
    result = match_trunks(TrunkHints(trunk_name="GO2NET SD"), all_trunks(tmp_path))
    assert result.trunk_name == "GO2NET SD"
    assert not result.needs_human


def test_shared_prefix_without_carrier_is_ambiguous(tmp_path):
    # COM-NET and APPLE both use tech prefix 1111
    result = match_trunks(TrunkHints(tech_prefix="1111"), all_trunks(tmp_path))
    assert result.ambiguous
    assert result.needs_human


def test_shared_prefix_disambiguated_by_carrier(tmp_path):
    result = match_trunks(TrunkHints(tech_prefix="1111"), all_trunks(tmp_path), sender_carrier=48)
    assert result.trunk_id == 59
    assert not result.needs_human


def test_ambiguous_names_ask_human():
    trunks = [
        {"trunk_id": 1, "carrier_id": 1, "carrier_name": "C",
         "trunk_name": "TRUNK ALPHA 1001", "prefix": "", "cost_deck": ""},
        {"trunk_id": 2, "carrier_id": 1, "carrier_name": "C",
         "trunk_name": "TRUNK ALPHA 1002", "prefix": "", "cost_deck": ""},
    ]
    result = match_trunks(TrunkHints(trunk_name="TRUNK ALPHA 1000"), trunks)
    assert result.needs_human
