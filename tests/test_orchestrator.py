import pathlib
import sys
from email.message import EmailMessage

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import config
import fetcher
import orchestrator
from store import Store

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EMAILS = FIXTURES / "emails"
DECK_HEADER = ("prefix,destination,inter_rate,intra_rate,indeterminate_rate,connect_fee,"
               "init_duration,increment,grace_time,effective_date")


class FakePortal:
    def __init__(self, upload_ok=True, assign_ok=True):
        self.created = []
        self.uploaded = []
        self.assigned = []
        self.deleted = []
        self.upload_ok = upload_ok
        self.assign_ok = assign_ok
        self.existing = None

    def create_rate_deck(self, name, **kwargs):
        self.created.append((name, kwargs))
        return 999

    def upload_rate_deck(self, deck_id, path, **kwargs):
        self.uploaded.append((deck_id, str(path), kwargs))
        return {"ok": self.upload_ok, "deck_id": deck_id, "screenshot": "x"}

    def assign_deck_to_trunk(self, carrier_id, trunk_id, deck_id, *, expect_name=None):
        self.assigned.append((carrier_id, trunk_id, deck_id, expect_name))
        return {"ok": self.assign_ok, "cost_deck": expect_name if self.assign_ok else ""}

    def trunk_deck(self, carrier_id, trunk_id):
        return ""

    def delete_rate_deck(self, deck_id):
        self.deleted.append(deck_id)
        return True

    def existing_deck_id(self, carrier_id, trunk_id):
        return self.existing


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.jsonl")
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    return store


def collector():
    lines = []
    return lines, lines.append


def make_email(sender, subject, body, filename, csv_text, message_id):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "dummy@instacall.digital"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 03 Nov 2026 09:00:00 +0000"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    msg.add_attachment(csv_text.encode(), maintype="text", subtype="csv", filename=filename)
    return msg.as_bytes()


def make_email_multi(sender, subject, body, attachments, message_id):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "dummy@instacall.digital"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 03 Nov 2026 09:00:00 +0000"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    for filename, csv_text in attachments:
        msg.add_attachment(csv_text.encode(), maintype="text", subtype="csv", filename=filename)
    return msg.as_bytes()


def test_ani_dry_run_needs_human_without_queue(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    store.set_sender_domain("ani-network.net", 42)
    lines, notify = collector()

    result = orchestrator.process_email(store, (EMAILS / "ani.eml").read_bytes(),
                                        commit=False, notify=notify)

    assert result["status"] == "needs_human"
    assert store.list_pending("open") == []              # dry-run never enqueues


def test_ani_commit_enqueues_trunk_pending(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    store.set_sender_domain("ani-network.net", 42)
    portal = FakePortal()

    result = orchestrator.process_email(store, (EMAILS / "ani.eml").read_bytes(),
                                        portal=portal, commit=True, notify=collector()[1])

    assert result["status"] == "needs_human"
    pending = store.list_pending("open")
    assert len(pending) == 1 and pending[0]["kind"] == "trunk"
    assert not portal.created


def test_dialworld_download_dry_run(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)

    def fake_download(url, dest_dir, **kwargs):
        dest = pathlib.Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "6060-11-01.csv"
        out.write_text(DECK_HEADER + "\n1368200,NANPA,0.001253,0.001253,0.001253,0,12,6,,2026-11-01\n",
                       encoding="utf-8")
        return out

    monkeypatch.setattr(fetcher, "download_url", fake_download)
    lines, notify = collector()
    result = orchestrator.process_email(store, (EMAILS / "dialworld.eml").read_bytes(),
                                        commit=False, notify=notify)

    assert result["status"] == "dry_run"
    assert result["trunk_id"] == 82
    assert result["deck_name"] == "DW USA CONVO TRUNK 6060 11_01_2026"


def btcl_email(message_id="<btcl-1103@instacall.digital>"):
    return make_email("BTCL <rates@btcl.example>", "BTCL rate deck 2026-11-03",
                      "---------- Forwarded message ---------\nFrom: BTCL <rates@btcl.example>\n"
                      "Trunk: BTCL TNT TRUNK\nEffective: 2026-11-03\n",
                      "btcl.csv", DECK_HEADER + "\n1201200,NANPA,0.001,0.001,0.001,0,12,6,,2026-11-03\n",
                      message_id)


def test_commit_dev_trunk_uploads_and_assigns(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    portal = FakePortal()
    lines, notify = collector()

    result = orchestrator.process_email(store, btcl_email(), portal=portal, commit=True, notify=notify)

    assert result["status"] == "uploaded"
    assert result["deck_id"] == 999
    assert portal.created[0][0] == "BTCL TNT TRUNK 11_03_2026"
    assert portal.created[0][1]["lrn_billing"] is True
    assert portal.uploaded[0][0] == 999
    assert portal.uploaded[0][2]["replace"] is True
    assert portal.assigned[0][2] == 999
    assert portal.assigned[0][3] == "BTCL TNT TRUNK 11_03_2026"
    assert portal.deleted == []
    assert store.last_deck(87)["deck_id"] == 999


def test_reuse_deck_uploads_without_create_or_assign(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    portal = FakePortal()
    portal.existing = 555

    result = orchestrator.process_email(store, btcl_email("<btcl-reuse@x>"), portal=portal,
                                        commit=True, notify=collector()[1], reuse=True)

    assert result["status"] == "uploaded"
    assert result["deck_id"] == 555
    assert portal.created == []            # reused; no new deck created
    assert portal.assigned == []           # already assigned; not re-assigned
    assert portal.uploaded[0][0] == 555
    assert portal.deleted == []


def test_reuse_deck_without_existing_fails(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    portal = FakePortal()                  # existing stays None

    result = orchestrator.process_email(store, btcl_email("<btcl-reuse-missing@x>"), portal=portal,
                                        commit=True, notify=collector()[1], reuse=True)

    assert result["status"] == "upload_failed"
    assert portal.created == [] and portal.deleted == []


def test_assign_failure_removes_deck(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    portal = FakePortal(assign_ok=False)

    result = orchestrator.process_email(store, btcl_email("<btcl-assign-fail@x>"),
                                        portal=portal, commit=True, notify=collector()[1])

    assert result["status"] == "needs_human"
    assert portal.deleted == [999]


def test_upload_failure_removes_deck(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    portal = FakePortal(upload_ok=False)

    result = orchestrator.process_email(store, btcl_email("<btcl-upload-fail@x>"),
                                        portal=portal, commit=True, notify=collector()[1])

    assert result["status"] == "upload_failed"
    assert portal.deleted == [999]


def patch_dialworld_download(monkeypatch):
    def fake_download(url, dest_dir, **kwargs):
        dest = pathlib.Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "6060-11-01.csv"
        out.write_text(DECK_HEADER + "\n1368200,NANPA,0.001253,0.001253,0.001253,0,12,6,,2026-11-01\n",
                       encoding="utf-8")
        return out
    monkeypatch.setattr(fetcher, "download_url", fake_download)


def test_unapproved_trunk_enqueues_approval(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    patch_dialworld_download(monkeypatch)
    portal = FakePortal()

    result = orchestrator.process_email(store, (EMAILS / "dialworld.eml").read_bytes(),
                                        portal=portal, commit=True, notify=collector()[1])

    assert result["status"] == "needs_human"
    pending = store.list_pending("open")
    assert pending and pending[0]["kind"] == "approval" and pending[0]["trunk_id"] == 82
    assert not portal.created


def test_resolve_then_approve_commits_from_pending(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    store.set_sender_domain("ani-network.net", 42)
    portal = FakePortal()

    orchestrator.process_email(store, (EMAILS / "ani.eml").read_bytes(),
                               portal=portal, commit=True, notify=collector()[1])
    trunk_item = store.list_pending("open")[0]

    # human picks the trunk -> non-dev trunk still needs approval (no commit yet)
    store.resolve_pending(trunk_item["id"], trunk_id=43)
    assert orchestrator.process_pending(store, portal=portal, commit=True,
                                        notify=collector()[1]) == []
    approval = store.list_pending("open")[0]
    assert approval["kind"] == "approval"

    # human approves -> daemon commits on the next pass
    store.resolve_pending(approval["id"], approve=True)
    results = orchestrator.process_pending(store, portal=portal, commit=True, notify=collector()[1])
    assert results and results[0]["status"] == "uploaded"
    assert portal.created[0][0].startswith("ANI Network LD")
    assert store.get_pending(approval["id"])["status"] == "committed"
    assert store.last_deck(43) is not None


def test_require_clean_warnings_enqueues(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    raw = make_email("BTCL <rates@btcl.example>", "BTCL warn deck",
                     "Trunk: BTCL TNT TRUNK\nEffective: 2026-12-05\n",
                     "warn.csv", DECK_HEADER + "\n1212,NANPA,0.001,0.001,0.001,0,12,6,,2026-12-05\n",
                     "<btcl-warn@x>")
    portal = FakePortal()

    result = orchestrator.process_email(store, raw, portal=portal, commit=True,
                                        notify=collector()[1], require_clean_warnings=True)
    assert result["status"] == "needs_human"
    pending = store.list_pending("open")
    assert pending and pending[0]["kind"] == "warnings"
    assert not portal.created

    store.resolve_pending(pending[0]["id"], approve=True)
    results = orchestrator.process_pending(store, portal=portal, commit=True, notify=collector()[1])
    assert results and results[0]["status"] == "uploaded"


def test_duplicate_commit_is_skipped(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    portal = FakePortal()
    _, notify = collector()
    raw = btcl_email("<dup@x>")

    first = orchestrator.process_email(store, raw, portal=portal, commit=True, notify=notify)
    second = orchestrator.process_email(store, raw, portal=portal, commit=True, notify=notify)

    assert first["status"] == "uploaded"
    assert second["status"] == "duplicate"           # idempotency guards uploads


def test_dry_run_then_commit_is_not_duplicate(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    portal = FakePortal()
    raw = btcl_email("<dry-then-commit@x>")

    dry = orchestrator.process_email(store, raw, notify=collector()[1])
    assert dry["status"] == "dry_run"

    committed = orchestrator.process_email(store, raw, portal=portal, commit=True,
                                           notify=collector()[1])
    assert committed["status"] == "uploaded"          # a dry-run never blocks the commit


def test_validation_error_blocks_upload(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    raw = make_email("BTCL <rates@btcl.example>", "BTCL deck",
                     "Trunk: BTCL TNT TRUNK\nEffective: 2026-11-03\n",
                     "bad.csv", DECK_HEADER + "\n1201200,NANPA,abc,0.001,0.001,0,12,6,,2026-11-03\n",
                     "<btcl-bad@instacall.digital>")
    portal = FakePortal()
    lines, notify = collector()

    result = orchestrator.process_email(store, raw, portal=portal, commit=True, notify=notify)

    assert result["status"] == "validation_error"
    assert not portal.uploaded
    assert any("validation failed" in line for line in lines)


def multi_deck_email(message_id="<multi-1@x>"):
    return make_email_multi(
        "Dial World <rates@dialworld.com>", "Dial World decks", "Effective: 2026-11-01\n",
        [("Prefix_6060 deck.csv",
          DECK_HEADER + "\n1368200,NANPA,0.001,0.001,0.001,0,12,6,,2026-11-01\n"),
         ("Prefix_1515 deck.csv",
          DECK_HEADER + "\n1368201,NANPA,0.002,0.002,0.002,0,12,6,,2026-11-01\n")],
        message_id)


def test_facts_for_file_extracts_prefix():
    from email_llm import EmailFacts
    facts = orchestrator._facts_for_file(EmailFacts(), pathlib.Path("Prefix_6060 deck.csv"))
    assert facts.tech_prefix == "6060"
    assert facts.deck_filename == "Prefix_6060 deck.csv"
    kept = orchestrator._facts_for_file(EmailFacts(tech_prefix="9999"), pathlib.Path("plain.csv"))
    assert kept.tech_prefix == "9999"


def test_multi_deck_email_dry_run(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    result = orchestrator.process_email(store, multi_deck_email(), notify=collector()[1])

    assert result["status"] == "multi"
    assert [r["status"] for r in result["results"]] == ["dry_run", "dry_run"]
    assert sorted(r["trunk_id"] for r in result["results"]) == [82, 83]  # DW 6060 + DW 1515


def test_multi_deck_commit_uploads_both(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    store.approve(82, by="tester", carrier_id=58)
    store.approve(83, by="tester", carrier_id=58)
    portal = FakePortal()

    result = orchestrator.process_email(store, multi_deck_email("<multi-2@x>"),
                                        portal=portal, commit=True, notify=collector()[1])

    assert result["status"] == "multi"
    assert [r["status"] for r in result["results"]] == ["uploaded", "uploaded"]
    assert len(portal.created) == 2


def test_auto_learn_sender_domain(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    patch_dialworld_download(monkeypatch)

    orchestrator.process_email(store, (EMAILS / "dialworld.eml").read_bytes(),
                               notify=collector()[1])

    assert store.carrier_for_domain("dialworld.com") == 58   # learned from the resolved trunk


def test_unmapped_layout_guard(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    raw = make_email("BTCL <rates@btcl.example>", "mystery deck", "Trunk: BTCL TNT TRUNK\n",
                     "mystery.csv", "Foo,Bar,Baz\n1,2,3\n", "<mystery@x>")

    dry = orchestrator.process_email(store, raw, notify=collector()[1])
    assert dry["status"] == "needs_mapping"
    assert set(dry["missing"]) == {"prefix", "inter_rate", "intra_rate"}
    assert store.list_pending("open") == []                   # dry-run: no queue

    raw2 = make_email("BTCL <rates@btcl.example>", "mystery deck", "Trunk: BTCL TNT TRUNK\n",
                      "mystery.csv", "Foo,Bar,Baz\n1,2,3\n", "<mystery2@x>")
    portal = FakePortal()
    committed = orchestrator.process_email(store, raw2, portal=portal, commit=True,
                                           notify=collector()[1])
    assert committed["status"] == "needs_mapping"
    pending = store.list_pending("open")
    assert pending and pending[0]["kind"] == "mapping"
    assert not portal.created


def test_unmapped_layout_records_llm_suggestion(tmp_path, monkeypatch):
    import json

    store = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(orchestrator, "propose_mapping",
                        lambda headers, sample_rows=None, **kw: {"prefix": ["Foo"]})
    raw = make_email("BTCL <rates@btcl.example>", "mystery deck", "Trunk: BTCL TNT TRUNK\n",
                     "mystery.csv", "Foo,Bar,Baz\n1,2,3\n", "<mystery-llm@x>")

    result = orchestrator.process_email(store, raw, portal=FakePortal(), commit=True,
                                        notify=collector()[1])

    assert result["status"] == "needs_mapping"
    assert result["suggested_aliases"] == {"prefix": ["Foo"]}
    reasons = json.loads(store.list_pending("open")[0]["reasons"])
    assert 'suggested_aliases: {"prefix": ["Foo"]}' in reasons
    assert any(reason.startswith("suggestion still missing:") for reason in reasons)


def test_process_pending_skips_mapping_items(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch)
    run_id = store.start_run("<map@x>")
    store.add_pending("<map@x>", kind="mapping", run_id=run_id, trunk_id=87, reasons=[])
    item = store.list_pending("open")[0]
    store.resolve_pending(item["id"])
    portal = FakePortal()

    results = orchestrator.process_pending(store, portal=portal, commit=True, notify=collector()[1])

    assert results == []
    assert store.get_pending(item["id"])["status"] == "applied"
    assert not portal.created and not portal.uploaded


def test_effective_date_mismatch_needs_approval(tmp_path, monkeypatch):
    import json as _json

    store = setup(tmp_path, monkeypatch)
    raw = make_email("BTCL <rates@btcl.example>", "BTCL deck",
                     "Trunk: BTCL TNT TRUNK\nEffective: 2026-11-15\n",
                     "deck.csv",
                     DECK_HEADER + "\n1201200,NANPA,0.001,0.001,0.001,0,12,6,,2026-11-01\n",
                     "<btcl-date-mismatch@x>")

    result = orchestrator.process_email(store, raw, portal=FakePortal(), commit=True,
                                        notify=collector()[1], require_clean_warnings=True)

    assert result["status"] == "needs_human"
    reasons = _json.loads(store.list_pending("open")[0]["reasons"])
    assert any("effective_date mismatch" in reason for reason in reasons)


def test_count_changes(tmp_path, monkeypatch):
    import pandas as pd
    from mapper import TEMPLATE_COLUMNS

    previous = pd.DataFrame(
        [["1201200", "NANPA", "0.001", "0.001", "0.001", "0", "12", "6", "", ""]],
        columns=TEMPLATE_COLUMNS, dtype=str)
    current = pd.DataFrame(
        [["1201200", "NANPA", "0.002", "0.001", "0.002", "0", "12", "6", "", ""],
         ["1201201", "NANPA", "0.001", "0.001", "0.001", "0", "12", "6", "", ""]],
        columns=TEMPLATE_COLUMNS, dtype=str)

    assert orchestrator.count_changes(previous, current) == 2   # one changed, one new
