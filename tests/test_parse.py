import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from parse import carrier_name, extract_carriers, extract_decks, extract_trunks

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CARRIER_41 = (FIXTURES / "carrier_41_trunks.html").read_text(encoding="utf-8")
CARRIERS = (FIXTURES / "carriers.html").read_text(encoding="utf-8")


def test_extract_trunks_from_real_card_page():
    trunks = extract_trunks(CARRIER_41)
    assert [t["trunk_name"] for t in trunks] == ["GO2NET SD", "GO2NET LD"]
    assert [t["trunk_id"] for t in trunks] == ["63", "64"]
    assert trunks[0]["prefix"] == ""
    assert trunks[1]["prefix"] == "7979"
    assert trunks[0]["cost_deck"] == "GO2NET SD 6/24/2026"
    assert trunks[1]["cost_deck"] == "GO2NET LD"
    assert [t["status"] for t in trunks] == ["Active", "Active"]


def test_extract_trunks_ignores_ip_table():
    trunks = extract_trunks(CARRIER_41)
    names = [t["trunk_name"] for t in trunks]
    assert "203.0.113.10" not in names
    assert "Add IP" not in names


def test_extract_trunks_empty():
    assert extract_trunks("<html></html>") == []


def test_btcl_card():
    html = ('<div class="cpanel"><div class="cpanel-head">'
            '<strong>BTCL TNT TRUNK</strong>'
            '<span class="cbadge active">Active</span>'
            '<a href="/carrier_trunks.php?id=59&amp;edit=70">Edit</a></div></div>')
    trunks = extract_trunks(html)
    assert len(trunks) == 1
    assert trunks[0]["trunk_name"] == "BTCL TNT TRUNK"
    assert trunks[0]["trunk_id"] == "70"
    assert trunks[0]["status"] == "Active"


def test_carrier_name():
    assert carrier_name(CARRIER_41, "fallback") == "GO2NET LLC"


def test_extract_carriers():
    assert [c["carrier_id"] for c in extract_carriers(CARRIERS)] == [35, 41, 47]
    assert extract_carriers("<html></html>") == []


DECK_HTML = (FIXTURES / "rate_decks_excerpt.html").read_text(encoding="utf-8")


def test_extract_decks():
    decks = extract_decks(DECK_HTML)
    assert [d["deck_id"] for d in decks] == [175, 72]
    assert decks[0]["name"] == "ALASKA RATE"
    assert decks[0]["type"] == "Customer"
    assert decks[1]["name"] == "BTCL TNT TRUNK 11_03_2026"
    assert decks[1]["type"] == "Carrier"
    assert decks[1]["mode"] == "NPANXX"
    assert decks[1]["status"] == "Active"
    assert extract_decks("<html></html>") == []
