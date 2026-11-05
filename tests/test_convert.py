"""Offline conversion tests for M2 (cleaner + mapper + validator + convert CLI)."""
import pathlib
import sys
import zipfile

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from cleaner import read_clean
from convert import load_trunk_config, main
from mapper import TEMPLATE_COLUMNS, parse_effective_date, to_template
from validator import has_errors, validate

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ANI = FIXTURES / "ANI_10_28_2026_rates.csv"
BLC = FIXTURES / "BLC_Platinum_Plus_Q3_2026_11_02.csv"
DIAL = FIXTURES / "DIAL WORLD  Prefix_6060 11_01_2026.csv"
FRACTEL = FIXTURES / "Fractel-2026-11-01-CVDeluxe-Domestic.csv"
CONVO = FIXTURES / "RATES-CONVO_2020_NPANXX_12_6-2026-11-01.csv"

ALL_FIXTURES = [ANI, BLC, DIAL, FRACTEL, CONVO]


def convert(path, trunk_config=None):
    return to_template(read_clean(path), trunk_config)


def by_prefix(df):
    return {str(row[0]): row for row in df.itertuples(index=False, name=None)}


# --- cleaner ---------------------------------------------------------------

def test_bom_stripped():
    df = read_clean(CONVO)
    assert list(df.columns)[0] == "Name"
    assert to_template(df).iloc[0]["prefix"] == "1368200"


def test_blank_rows_removed():
    expected = {ANI: 13, BLC: 5, DIAL: 13, FRACTEL: 12, CONVO: 12}
    for path, count in expected.items():
        assert len(convert(path)) == count, path.name


def test_fractel_hash_header_detected():
    df = read_clean(FRACTEL)
    assert list(df.columns) == [
        "Code", "Interstate", "Intrastate", "Indeterminate", "Destination", "EffectiveDateUTC",
    ]


def test_zip_input(tmp_path):
    text = "Prefix,Inter Rate,Intra Rate\n1201200,0.0005,0.0009\n"
    archive = tmp_path / "deck.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("rates/deck.csv", text)
    row = by_prefix(convert(archive))["1201200"]
    assert row[4] == "0.0009"


def test_xlsx_input(tmp_path):
    frame = pd.DataFrame([["1201200", "0.0005", "0.0009"]],
                         columns=["Prefix", "Inter Rate", "Intra Rate"])
    path = tmp_path / "deck.xlsx"
    frame.to_excel(path, index=False)
    row = by_prefix(convert(path))["1201200"]
    assert row[2] == "0.0005" and row[4] == "0.0009"


# --- mapper ----------------------------------------------------------------

def test_header_equals_template():
    for path in ALL_FIXTURES:
        assert list(convert(path).columns) == TEMPLATE_COLUMNS, path.name


def test_blc_verified_expectation():
    rows = by_prefix(convert(BLC))
    verified = {
        "1201200": "1201200,NANPA,0.0012,0.0012,0.0012,0,12,6,,",
        "1201201": "1201201,NANPA,0.00234,0.00234,0.00234,0,12,6,,",
        "1201202": "1201202,NANPA,0.0012,0.0012,0.0012,0,12,6,,",
    }
    for prefix, expected in verified.items():
        row = rows[prefix]
        assert ",".join(row[:9]) + "," == expected
        assert row[9] == "2026-11-02"


def test_convo_equals_dial_world_on_shared_prefixes():
    convo, dial = by_prefix(convert(CONVO)), by_prefix(convert(DIAL))
    shared = set(convo) & set(dial)
    assert shared
    for prefix in shared:
        assert convo[prefix][0] == dial[prefix][0]
        assert convo[prefix][2:4] == dial[prefix][2:4]


def test_fractel_row_and_defaults():
    row = by_prefix(convert(FRACTEL))["1201200"]
    assert row[1] == "NANPA"
    assert row[2] == "0.001174" and row[3] == "0.00132" and row[4] == "0.001335"
    assert row[5] == "0" and row[6] == "12" and row[7] == "6"
    assert row[8] == ""
    assert row[9] == "2026-11-01"


def test_destination_always_nanpa():
    for path in ALL_FIXTURES:
        assert set(convert(path)["destination"]) == {"NANPA"}, path.name


def test_rates_stay_exact_strings():
    rows = by_prefix(convert(ANI))
    assert rows["1201200"][2] == "0.0013"
    assert rows["1201202"][2] == "0.0005"
    assert rows["1201203"][2] == "0.001"


def test_ani_defaults_and_grace_and_no_date():
    row = by_prefix(convert(ANI))["1201200"]
    assert row[5] == "0"            # connect fee present
    assert row[6] == "12" and row[7] == "6"
    assert row[8] == "0"            # grace time present
    assert row[9] == ""             # no date column -> blank


def test_indeterminate_is_max_when_missing(tmp_path):
    path = tmp_path / "two_cols.csv"
    path.write_text("Prefix,Inter Rate,Intra Rate\n1201200,0.0005,0.0009\n1201201,0.0030,0.0010\n",
                    encoding="utf-8")
    rows = by_prefix(convert(path))
    assert rows["1201200"][4] == "0.0009"   # intra larger, original text
    assert rows["1201201"][4] == "0.0030"   # inter larger, original text kept


@pytest.mark.parametrize("text,expected", [
    ("11/2/2026", "2026-11-02"),
    ("11/1/2026", "2026-11-01"),
    ("11/1/2026 0:00", "2026-11-01"),
    ("2026-11-01 00:00:00", "2026-11-01"),
    ("11/2/26", "2026-11-02"),
    ("", ""),
])
def test_parse_effective_date(text, expected):
    assert parse_effective_date(text) == expected


def test_trunk_config_defaults_and_aliases(tmp_path):
    path = tmp_path / "custom.csv"
    path.write_text("mydial,inter,intra\n1201200,0.001,0.002\n", encoding="utf-8")
    cfg = {"default_init_duration": 6, "default_increment": 1,
           "header_aliases": {"prefix": ["mydial"]}}
    row = by_prefix(convert(path, cfg))["1201200"]
    assert row[0] == "1201200"
    assert row[4] == "0.002"
    assert row[6] == "6" and row[7] == "1"


def test_load_trunk_config_yaml(tmp_path):
    path = tmp_path / "trunk.yaml"
    path.write_text("default_increment: 1\nheader_aliases:\n  prefix: [mydial]\n", encoding="utf-8")
    cfg = load_trunk_config(str(path))
    assert cfg["default_increment"] == 1
    assert cfg["header_aliases"]["prefix"] == ["mydial"]


# --- validator -------------------------------------------------------------

def make_template(rows):
    return pd.DataFrame(rows, columns=TEMPLATE_COLUMNS, dtype=str)


def good_row(prefix, inter="0.001", intra="0.001", ind="0.001", init="0", duration="12", inc="6"):
    return [prefix, "NANPA", inter, intra, ind, init, duration, inc, "", ""]


def codes(issues):
    return {i.code for i in issues}


def test_validator_clean_deck_passes():
    df = make_template([good_row("1201200"), good_row("1201201", inter="0.002", intra="0.002", ind="0.002")])
    assert validate(df) == []
    assert not has_errors(validate(df))


def test_validator_duplicate_prefix():
    df = make_template([good_row("1201200"), good_row("1201200")])
    issues = validate(df)
    assert "duplicate_prefix" in codes(issues)
    assert has_errors(issues)


def test_validator_bad_and_empty_rate():
    assert "bad_rate" in codes(validate(make_template([good_row("1201200", inter="abc")])))
    assert "bad_rate" in codes(validate(make_template([good_row("1201200", inter="-0.1")])))
    assert "empty_rate" in codes(validate(make_template([good_row("1201200", inter="")])))


def test_validator_prefix_digits_error_and_length_warning():
    assert "bad_prefix" in codes(validate(make_template([good_row("12a0120")])))
    for odd in ("2201200", "1212"):        # non-NPANXX length -> warning, not a blocker
        issues = validate(make_template([good_row(odd)]))
        assert "prefix_length" in codes(issues)
        assert not has_errors(issues)


def test_validator_bad_int_field():
    assert "bad_int_field" in codes(validate(make_template([good_row("1201200", duration="0")])))
    assert "bad_int_field" in codes(validate(make_template([good_row("1201200", inc="x")])))


def test_validator_bad_effective_date():
    row = good_row("1201200")
    row[9] = "not-a-date"
    assert "bad_effective_date" in codes(validate(make_template([row])))


def test_validator_header_mismatch():
    df = make_template([good_row("1201200")]).drop(columns=["grace_time"])
    assert "header_mismatch" in codes(validate(df))


def test_validator_previous_row_drop_warns():
    previous = make_template([good_row(f"120120{i}") for i in range(10)])
    current = make_template([good_row("1201200"), good_row("1201201")])
    issues = validate(current, previous=previous)
    assert "row_count_drop" in codes(issues)
    assert not has_errors(issues)


def test_validator_previous_rate_change_warns():
    previous = make_template([good_row(f"12012{i:02d}", "0.001", "0.001", "0.001") for i in range(100)])
    current = make_template([good_row(f"12012{i:02d}", "0.010", "0.010", "0.010") for i in range(100)])
    issues = validate(current, previous=previous)
    assert "rate_change" in codes(issues)
    assert not has_errors(issues)


# --- convert CLI -----------------------------------------------------------

def test_cli_writes_on_success(tmp_path):
    source = tmp_path / "good.csv"
    source.write_text("Prefix,Inter Rate,Intra Rate\n1201200,0.0005,0.0009\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    assert main([str(source), "-o", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(TEMPLATE_COLUMNS)
    assert lines[1] == "1201200,NANPA,0.0005,0.0009,0.0009,0,12,6,,"


def test_cli_blocks_on_error(tmp_path):
    source = tmp_path / "bad.csv"
    source.write_text("Prefix,Inter Rate,Intra Rate\n1201200,abc,0.001\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    assert main([str(source), "-o", str(out)]) == 1
    assert not out.exists()


def test_missing_required_columns():
    from mapper import missing_required_columns

    assert missing_required_columns(["Foo", "Bar", "Baz"]) == ["prefix", "inter_rate", "intra_rate"]
    assert missing_required_columns(["Prefix", "Inter Rate", "Intra Rate"]) == []
    assert missing_required_columns(["Code", "Interstate", "Intrastate"]) == []


def test_explicit_none_defaults_fall_back():
    frame = pd.DataFrame({"Prefix": ["1201200"], "Inter Rate": ["0.001"], "Intra Rate": ["0.001"]})

    out = to_template(frame, {"default_init_duration": None, "default_increment": None})

    assert out.iloc[0]["init_duration"] == "12"      # not the string "None"
    assert out.iloc[0]["increment"] == "6"
