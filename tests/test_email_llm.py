import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from email_llm import extract_facts, propose_mapping, rule_based_facts
from mailbox import parse_email
from store import Store
from trunk_matcher import match_trunks

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EMAILS = FIXTURES / "emails"


def facts(name):
    return rule_based_facts(parse_email((EMAILS / name).read_bytes()))


def parsed(name):
    return parse_email((EMAILS / name).read_bytes())


def test_dialworld_rules():
    result = facts("dialworld.eml")
    assert result.tech_prefix == "6060"
    assert "USA CONVO" in result.trunk_name
    assert result.effective_date == "2026-11-01"
    assert result.sender_domain == "dialworld.com"


def test_nuso_rules_pick_newest_attachment_and_increments():
    result = facts("nuso.eml")
    assert result.deck_filename.endswith(".csv.zip")
    assert "_10_15" not in result.deck_filename          # not the superseded notice
    assert (result.init_duration, result.increment) == (12, 6)
    assert result.effective_date == "2026-11-01"
    assert result.sender_domain == "nuso.com"


def test_ani_rules_have_no_trunk():
    result = facts("ani.eml")
    assert result.sender_domain == "ani-network.net"
    assert result.tech_prefix == "" and result.trunk_name == ""
    assert result.deck_filename == "ANI_10_28_2026_rates.csv"
    assert result.effective_date == "2026-10-29"         # "effective Oct 29, 2026"


def test_fractel_rules():
    result = facts("fractel.eml")
    assert result.effective_date == "2026-11-01"
    assert result.deck_filename.endswith(".csv")
    assert result.sender_domain == "fractel.com"


def test_dialworld_facts_match_real_trunk(tmp_path):
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    result = match_trunks(facts("dialworld.eml").to_hints(), store.all_trunks())
    assert result.trunk_name == "DW USA CONVO TRUNK 6060"


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, system, user):
        return self.payload


def test_llm_overlays_and_validates():
    payload = '{"carrier_name": "ANI Network", "trunk_name": "ANI Network LD", "effective_date": "2026-10-29"}'
    result = extract_facts(parsed("ani.eml"), client=FakeClient(payload), use_llm=True)
    assert result.carrier_name == "ANI Network"
    assert result.trunk_name == "ANI Network LD"
    assert result.sender_domain == "ani-network.net"                 # rule value preserved
    assert result.deck_filename == "ANI_10_28_2026_rates.csv"


def test_llm_ignores_unknown_keys():
    payload = '{"tech_prefix": "6060", "invented": "x"}'
    result = extract_facts(parsed("dialworld.eml"), client=FakeClient(payload), use_llm=True)
    assert result.tech_prefix == "6060"


def test_llm_network_failure_falls_back_to_rules():
    class Boom:
        def complete_json(self, system, user):
            raise RuntimeError("network down")

    result = extract_facts(parsed("dialworld.eml"), client=Boom(), use_llm=True)
    assert result.tech_prefix == "6060"


def test_llm_invalid_json_falls_back_to_rules():
    result = extract_facts(parsed("dialworld.eml"), client=FakeClient("not json at all"), use_llm=True)
    assert result.tech_prefix == "6060"
    assert result.effective_date == "2026-11-01"


def test_propose_mapping_validates_against_headers():
    payload = '{"prefix": "Dialcode", "inter_rate": "Rate (inter)", "intra_rate": "Nope"}'
    result = propose_mapping(["Dialcode", "Rate (inter)", "Intrastate"],
                             client=FakeClient(payload), use_llm=True)
    assert result == {"prefix": ["Dialcode"], "inter_rate": ["Rate (inter)"]}  # "Nope" dropped


def test_propose_mapping_disabled_without_llm():
    assert propose_mapping(["Dialcode"], use_llm=False) == {}


def test_propose_mapping_bad_json_returns_empty():
    assert propose_mapping(["Dialcode"], client=FakeClient("not json"), use_llm=True) == {}


def test_to_hints_includes_deck_name():
    from email_llm import EmailFacts

    assert EmailFacts(deck_filename="BTCL TNT Domestic.csv").to_hints().deck_name == "BTCL TNT Domestic"
    assert EmailFacts(deck_filename="").to_hints().deck_name == ""
