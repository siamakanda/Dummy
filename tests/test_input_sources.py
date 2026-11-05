import datetime as dt
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import config
import orchestrator
from input_sources import DirectorySource, EmailSource, safe_name
from store import Store

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EMAILS = FIXTURES / "emails"
BLC = FIXTURES / "BLC_Platinum_Plus_Q3_2026_11_02.csv"


def test_directory_source_csv(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / BLC.name).write_bytes(BLC.read_bytes())

    items = DirectorySource(inbox).fetch(tmp_path / "archive")

    assert len(items) == 1
    item = items[0]
    assert item.origin == "directory"
    assert item.facts.deck_filename == BLC.name
    assert item.facts.effective_date == "2026-11-02"       # "11/2/2026" normalized
    assert item.deck_files and item.deck_files[0].exists()


def test_directory_source_extracts_zip(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    with zipfile.ZipFile(inbox / "deck.zip", "w") as zf:
        zf.writestr("inner/BLC.csv", BLC.read_text(encoding="utf-8-sig"))

    items = DirectorySource(inbox).fetch(tmp_path / "archive")

    assert len(items) == 1
    assert any(p.suffix == ".csv" for p in items[0].deck_files)


def test_directory_sidecar_sets_hints(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    deck = inbox / "mystery.csv"
    deck.write_bytes(BLC.read_bytes())
    pathlib.Path(str(deck) + ".json").write_text(
        '{"trunk_name": "BTCL TNT TRUNK", "tech_prefix": "6060"}', encoding="utf-8")

    item = DirectorySource(inbox).fetch(tmp_path / "archive")[0]

    assert item.facts.trunk_name == "BTCL TNT TRUNK"
    assert item.facts.tech_prefix == "6060"


def test_directory_trunk_override_in_meta(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / BLC.name).write_bytes(BLC.read_bytes())

    item = DirectorySource(inbox, trunk_override="BTCL TNT TRUNK").fetch(tmp_path / "archive")[0]

    assert item.meta["trunk_override"] == "BTCL TNT TRUNK"


def test_directory_is_idempotent(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / BLC.name).write_bytes(BLC.read_bytes())
    store = Store(tmp_path / "dummy.db")
    source = DirectorySource(inbox)

    first = source.fetch(tmp_path / "archive", store=store)
    assert len(first) == 1
    run_id = store.start_run(first[0].source_id)
    store.finish_run(run_id, status="uploaded")          # only uploads are skipped

    assert source.fetch(tmp_path / "archive", store=store) == []


class FakeMailbox:
    def __init__(self, raw):
        self.raw = raw

    def fetch_raw(self, criteria="UNSEEN"):
        return [self.raw]


def test_email_source(tmp_path):
    raw = (EMAILS / "ani.eml").read_bytes()
    items = EmailSource(FakeMailbox(raw)).fetch(tmp_path / "archive")

    assert len(items) == 1
    assert items[0].origin == "email"
    assert items[0].facts.sender_domain == "ani-network.net"
    assert any(p.suffix == ".csv" for p in items[0].deck_files)


def test_safe_name_bounds_long_message_ids():
    long_id = "TeamsMissedActivityEmailTFL-" + "a" * 200 + "@odspnotify"

    name = safe_name(long_id)

    assert len(name) <= 80                                   # Windows-safe
    assert name == safe_name(long_id)                        # deterministic
    assert safe_name(long_id) != safe_name(long_id + "x")    # unique
    assert all(c.isalnum() or c in "._@-" for c in name)


def test_archive_dir_is_length_bounded():
    directory = orchestrator.archive_dir("x" * 500, dt.date(2026, 11, 5))

    assert len(directory.name) <= 80


def test_email_source_limit_keeps_newest(tmp_path):
    class MultiMailbox:
        def fetch_raw(self, criteria="UNSEEN"):
            return [b"m1", b"m2", b"m3"]

    items = EmailSource(MultiMailbox()).fetch(tmp_path / "archive", limit=2)

    assert len(items) == 2


def test_directory_source_limit(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("a.csv", "b.csv", "c.csv"):
        (inbox / name).write_bytes(BLC.read_bytes())

    items = DirectorySource(inbox).fetch(tmp_path / "archive", limit=2)

    assert len(items) == 2


def test_email_source_long_message_id(tmp_path):
    raw = (EMAILS / "ani.eml").read_bytes().replace(
        b"<ani-100740-1001@instacall.digital>",
        b"<TeamsMissedActivityEmailTFL-" + b"a" * 200 + b"@odspnotify>")

    items = EmailSource(FakeMailbox(raw)).fetch(tmp_path / "archive")

    assert len(items) == 1
    assert len(items[0].work_dir.name) <= 80


def test_directory_end_to_end_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.jsonl")
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / BLC.name).write_bytes(BLC.read_bytes())

    results = orchestrator.run_once(
        store, DirectorySource(inbox, trunk_override="BTCL TNT TRUNK"),
        commit=False, today=dt.date(2026, 11, 4))

    assert len(results) == 1
    assert results[0]["status"] == "dry_run"
    assert results[0]["trunk_id"] == 87
    assert results[0]["deck_name"] == "BTCL TNT TRUNK 11_02_2026"
    assert pathlib.Path(results[0]["converted_path"]).exists()
