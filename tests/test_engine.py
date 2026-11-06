"""Tests for the folder-first engine (offline; no portal, no browser)."""
import pathlib
import re
import sys
import time
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import config
import engine
from store import Store

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BLC = FIXTURES / "BLC_Platinum_Plus_Q3_2026_11_02.csv"
DEV_TRUNK = "BTCL TNT TRUNK"


def make_store(tmp_path):
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    return store


def test_job_roundtrip(tmp_path):
    store = make_store(tmp_path)

    job_id = store.add_job("file:abc", file_path="x.csv", file_name="x.csv",
                           candidates=[{"trunk_id": 87, "trunk_name": DEV_TRUNK, "score": 0.9}])

    assert job_id is not None
    assert store.add_job("file:abc") is None                 # idempotent by source_id
    job = store.get_job(job_id)
    assert job["status"] == "unmapped" and job["candidates"][0]["trunk_id"] == 87

    store.set_job_mapping(job_id, 87)
    assert store.get_job(job_id)["status"] == "mapped"
    store.update_job(job_id, status="dry_run", rows=3)
    assert store.get_job(job_id)["rows"] == 3


def test_scan_map_run_push_dry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / BLC.name).write_bytes(BLC.read_bytes())
    store = make_store(tmp_path)

    engine.cmd_scan(store, NS(dir=None))
    jobs = store.list_jobs()
    assert len(jobs) == 1
    job_id = jobs[0]["job_id"]

    engine.cmd_map(store, NS(job=job_id, trunk=DEV_TRUNK))
    assert store.get_job(job_id)["trunk_id"] == 87

    engine.cmd_run(store, NS(job=None))
    job = store.get_job(job_id)
    assert job["status"] == "dry_run"
    assert job["rows"] > 0
    assert pathlib.Path(job["converted_path"]).exists()

    engine.cmd_upload(store, NS(job=None, commit=False, headed=False))   # dry-run: no write
    assert store.get_job(job_id)["status"] == "dry_run"


def test_upload_allowed_on_any_trunk(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)          # converted, dry_run
    store.set_job_mapping(job_id, 82)                           # switch to a non-dev trunk
    store.update_job(job_id, status="dry_run")
    portal = FakePortal()

    results = engine.upload_jobs(store, portal, commit=True, job_id=job_id)

    assert results[0]["status"] == "uploaded"                   # upload is not gated
    assert portal.created
    store.close()


def test_assign_refuses_unapproved_trunk(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    engine.upload_jobs(store, FakePortal(), commit=True, job_id=job_id)  # upload on dev trunk
    store.set_job_mapping(job_id, 82)                           # now a non-dev/unapproved trunk
    store.update_job(job_id, status="uploaded")

    results = engine.assign_jobs(store, FakePortal(), commit=True, job_id=job_id)

    assert results[0]["status"] == "refused"
    assert store.get_job(job_id)["status"] == "uploaded"        # unchanged
    store.close()


def test_assign_succeeds_after_approval(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    engine.upload_jobs(store, FakePortal(), commit=True, job_id=job_id)
    store.set_job_mapping(job_id, 82)
    store.update_job(job_id, status="uploaded")
    portal = FakePortal()
    assert engine.assign_jobs(store, portal, commit=True, job_id=job_id)[0]["status"] == "refused"

    store.approve(82, by="test")
    results = engine.assign_jobs(store, portal, commit=True, job_id=job_id)

    assert results[0]["status"] == "assigned"
    assert store.get_job(job_id)["status"] == "assigned"
    store.close()


def test_upload_twice_is_idempotent(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    first = FakePortal()
    assert engine.upload_jobs(store, first, commit=True, job_id=job_id)[0]["status"] == "uploaded"

    second = FakePortal()
    result = engine.upload_jobs(store, second, commit=True, job_id=job_id)

    assert result[0]["status"] == "skipped"
    assert second.created == []                       # no duplicate deck
    assert store.get_job(job_id)["deck_id"] == 999
    store.close()


def test_assign_twice_is_idempotent(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    engine.upload_jobs(store, FakePortal(), commit=True, job_id=job_id)
    first = FakePortal()
    assert engine.assign_jobs(store, first, commit=True, job_id=job_id)[0]["status"] == "assigned"

    second = FakePortal()
    result = engine.assign_jobs(store, second, commit=True, job_id=job_id)

    assert result[0]["status"] == "skipped"
    assert second.assigned == []
    store.close()


def test_convert_needs_date_then_apply_date(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "mystery_20261113.csv").write_text(
        "prefix,inter_rate,intra_rate\n1201200,0.0012,0.0012\n", encoding="utf-8")

    store = make_store(tmp_path)
    engine.cmd_scan(store, NS(dir=None))
    job_id = store.list_jobs()[0]["job_id"]
    engine.cmd_map(store, NS(job=job_id, trunk=DEV_TRUNK))
    engine.cmd_run(store, NS(job=job_id))

    job = store.get_job(job_id)
    assert job["status"] == "needs_date"
    assert job["date_hint"] == "2026-11-13"          # filename hint, not applied automatically

    result = engine.apply_date(store, job_id, "2026-11-13")

    assert result["status"] == "dry_run"
    assert "11_13_2026" in store.get_job(job_id)["deck_name"]
    store.close()


def test_redo_upload_deletes_unassigned_deck(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    engine.upload_jobs(store, FakePortal(), commit=True, job_id=job_id)
    portal = FakePortal()

    result = engine.redo_upload(store, portal, store.get_job(job_id))

    assert result["status"] == "dry_run"              # reconverted with its known date
    assert portal.deleted == [999]
    job = store.get_job(job_id)
    assert job["deck_id"] is None and job["status"] == "dry_run"
    store.close()


def test_redo_refuses_assigned_deck(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    engine.upload_jobs(store, FakePortal(), commit=True, job_id=job_id)
    portal = FakePortal()
    portal.deck_assigned = True

    result = engine.redo_upload(store, portal, store.get_job(job_id))

    assert result["status"] == "refused"
    assert portal.deleted == []
    store.close()


def test_verify_deck_reports_presence_and_name(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    engine.upload_jobs(store, FakePortal(), commit=True, job_id=job_id)
    job = store.get_job(job_id)

    portal = FakePortal()
    portal.decks = [{"deck_id": 999, "name": job["deck_name"], "status": "enabled"}]
    result = engine.verify_deck(store, portal, job)
    assert result["status"] == "found" and result["name_matches"]

    portal.decks = []
    assert engine.verify_deck(store, portal, job)["status"] == "missing"
    store.close()


def test_convert_uses_previous_deck_for_warnings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "inbox").mkdir()
    store = make_store(tmp_path)

    prev = tmp_path / "prev.csv"
    header = ("prefix,destination,inter_rate,intra_rate,indeterminate_rate,connect_fee,"
              "init_duration,increment,grace_time,effective_date")
    lines = [header] + [f"1{i:06d},NANPA,0.001,0.001,0.001,0,12,6,," for i in range(100)]
    prev.write_text("\n".join(lines) + "\n", encoding="utf-8")
    prior_id = store.add_job("file:prev", file_path=str(prev), file_name="prev.csv")
    store.set_job_mapping(prior_id, 87)
    store.update_job(prior_id, status="dry_run", converted_path=str(prev), rows=100)

    (tmp_path / "inbox" / BLC.name).write_bytes(BLC.read_bytes())
    engine.cmd_scan(store, NS(dir=None))
    job_id = [j["job_id"] for j in store.list_jobs() if j["job_id"] != prior_id][0]
    engine.cmd_map(store, NS(job=job_id, trunk=DEV_TRUNK))
    engine.cmd_run(store, NS(job=job_id))

    job = store.get_job(job_id)
    assert job["status"] == "dry_run"
    assert job["notes"] != "0 warning(s)"             # row-count drop warning fired
    store.close()


def test_apply_suggested_mapping_converts_unknown_layout(tmp_path, monkeypatch):
    import email_llm
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "mystery.csv").write_text(
        "Alpha,Beta,Gamma\n1201200,0.0012,0.0012\n1201201,0.0023,0.0023\n",
        encoding="utf-8")
    monkeypatch.setattr(email_llm, "propose_mapping",
                        lambda headers, rows=None: {"prefix": ["Alpha"],
                                                    "inter_rate": ["Beta"],
                                                    "intra_rate": ["Gamma"]})

    store = make_store(tmp_path)
    engine.cmd_scan(store, NS(dir=None))
    job_id = store.list_jobs()[0]["job_id"]
    engine.cmd_map(store, NS(job=job_id, trunk=DEV_TRUNK))
    engine.cmd_run(store, NS(job=job_id))

    job = store.get_job(job_id)
    assert job["status"] == "needs_mapping"
    assert job["suggested_aliases"]["prefix"] == ["Alpha"]
    store.update_job(job_id, effective_date="2026-11-13")   # date is required before convert

    result = engine.apply_suggestion(store, job_id)

    assert result["status"] == "dry_run"
    assert store.get_job(job_id)["status"] == "dry_run"
    store.close()


def test_upload_and_assign_append_audit(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "AUDIT_LOG", audit)
    store, job_id = _mapped_job(tmp_path, monkeypatch)

    engine.upload_jobs(store, FakePortal(), commit=True, job_id=job_id)
    engine.assign_jobs(store, FakePortal(), commit=True, job_id=job_id)

    text = audit.read_text(encoding="utf-8")
    assert '"event": "upload"' in text and '"event": "assign"' in text
    store.close()


def test_job_manager_serialises_writes():
    import threading
    import time
    from engine_web import _JobManager

    jobs = _JobManager()
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(3)

    jobs.start("first", slow, write=True)
    assert started.wait(2)

    ran = []
    second = jobs.start("second", lambda: ran.append(1), write=True)
    for _ in range(150):
        if jobs.get(second)["status"] != "running":
            break
        time.sleep(0.02)

    assert jobs.get(second)["status"] == "done"
    assert ran == []                                   # rejected, never ran
    assert any(f["kind"] == "warning" for f in jobs.pop_flashes())
    release.set()


def _csrf(html: str) -> str:
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def _wait_job(client, response, timeout: float = 10.0) -> dict:
    """Actions are async: POST returns {job: id}; poll until the job finishes."""
    job_id = response.json()["job"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("status") != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


class FakePortal:
    def __init__(self, upload_ok=True, assign_ok=True):
        self.created, self.uploaded, self.assigned, self.deleted = [], [], [], []
        self.upload_ok, self.assign_ok = upload_ok, assign_ok
        self.decks = []
        self.deck_assigned = False

    def create_rate_deck(self, name, **kwargs):
        self.created.append((name, kwargs))
        return 999

    def upload_rate_deck(self, deck_id, path, **kwargs):
        self.uploaded.append((deck_id, str(path), kwargs))
        return {"ok": self.upload_ok, "deck_id": deck_id}

    def assign_deck_to_trunk(self, carrier_id, trunk_id, deck_id, *, expect_name=None):
        self.assigned.append((carrier_id, trunk_id, deck_id, expect_name))
        return {"ok": self.assign_ok}

    def delete_rate_deck(self, deck_id):
        self.deleted.append(deck_id)
        return True

    def list_decks(self):
        return self.decks

    def deck_is_assigned(self, deck_id):
        return self.deck_assigned


def _mapped_job(tmp_path, monkeypatch):
    """Scan + map + convert one deck; returns (store, job_id, portal-ready job)."""
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / BLC.name).write_bytes(BLC.read_bytes())
    store = make_store(tmp_path)
    engine.cmd_scan(store, NS(dir=None))
    job_id = store.list_jobs()[0]["job_id"]
    engine.cmd_map(store, NS(job=job_id, trunk=DEV_TRUNK))
    engine.cmd_run(store, NS(job=None))
    return store, job_id


def test_upload_job_creates_and_uploads_only(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    portal = FakePortal()

    results = engine.upload_jobs(store, portal, commit=True, job_id=job_id)

    assert results[0]["status"] == "uploaded"
    assert portal.created and portal.uploaded
    assert portal.assigned == []                       # step 1 must NOT assign
    job = store.get_job(job_id)
    assert job["status"] == "uploaded" and job["deck_id"] == 999
    assert job["deck_name"] and job["deck_created_at"]
    assert job["deck_name"] in job["notes"]
    store.close()


def test_upload_failure_removes_orphan(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    portal = FakePortal(upload_ok=False)

    results = engine.upload_jobs(store, portal, commit=True, job_id=job_id)

    assert results[0]["status"] == "failed"
    assert portal.deleted == [999]                     # created deck removed
    assert store.get_job(job_id)["status"] == "failed"
    store.close()


def test_assign_job_wires_the_uploaded_deck(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    engine.upload_jobs(store, FakePortal(), commit=True, job_id=job_id)   # step 1
    portal = FakePortal()

    results = engine.assign_jobs(store, portal, commit=True, job_id=job_id)  # step 2

    assert results[0]["status"] == "assigned"
    assert portal.assigned[0][2] == 999                # the recorded deck id
    assert store.get_job(job_id)["status"] == "assigned"
    store.close()


def test_assign_without_upload_fails(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)     # dry_run, no deck_id

    results = engine.assign_jobs(store, FakePortal(), commit=True, job_id=job_id)

    assert results[0]["status"] == "failed"
    assert "upload" in results[0]["error"].lower()
    store.close()


def test_engine_web_dashboard_and_map(tmp_path):
    import engine_web
    from fastapi.testclient import TestClient

    db = tmp_path / "dummy.db"
    store = make_store(tmp_path)
    job_id = store.add_job("file:w", file_path="w.csv", file_name="w.csv")
    store.close()

    client = TestClient(engine_web.create_app(db_path=db, secret="test-secret"))

    dash = client.get("/")                                   # no login required
    assert dash.status_code == 200 and "w.csv" in dash.text

    response = client.post(f"/map/{job_id}", data={"_csrf": _csrf(dash.text), "trunk_id": "87"})
    _wait_job(client, response)

    check = Store(db)
    assert check.get_job(job_id)["trunk_id"] == 87
    check.close()


def test_engine_web_assign_refuses_unapproved_trunk(tmp_path):
    """Assign must not launch a browser for a trunk it will refuse."""
    import engine_web
    from fastapi.testclient import TestClient

    db = tmp_path / "dummy.db"
    store = make_store(tmp_path)
    job_id = store.add_job("file:r", file_path="r.csv", file_name="r.csv")
    store.set_job_mapping(job_id, 82)                        # DW USA CONVO TRUNK 6060 - not dev/approved
    store.update_job(job_id, status="uploaded", deck_id=999)
    store.close()

    client = TestClient(engine_web.create_app(db_path=db, secret="test-secret"))
    dash = client.get("/")
    assert "r.csv" in dash.text

    response = client.post(f"/assign/{job_id}", data={"_csrf": _csrf(dash.text)})
    _wait_job(client, response)

    check = Store(db)
    assert check.get_job(job_id)["status"] == "uploaded"     # refused: still uploaded, not assigned
    check.close()


def test_engine_web_approve_trunk_unlocks_assign(tmp_path):
    import engine_web
    from fastapi.testclient import TestClient

    db = tmp_path / "dummy.db"
    store = make_store(tmp_path)
    job_id = store.add_job("file:a", file_path="a.csv", file_name="a.csv")
    store.set_job_mapping(job_id, 82)
    store.update_job(job_id, status="uploaded", deck_id=999)
    store.close()

    client = TestClient(engine_web.create_app(db_path=db, secret="test-secret"))
    dash = client.get("/")
    assert "Approve trunk" in dash.text                     # not approved yet

    response = client.post(f"/approve/{job_id}", data={"_csrf": _csrf(dash.text)})
    _wait_job(client, response)

    check = Store(db)
    assert check.is_approved(82)
    check.close()

    assert "Assign to trunk" in client.get("/").text        # now allowed


def test_engine_web_run_one_converts_a_mapped_job(tmp_path, monkeypatch):
    import engine_web
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / BLC.name).write_bytes(BLC.read_bytes())
    db = tmp_path / "dummy.db"
    store = make_store(tmp_path)
    engine.cmd_scan(store, NS(dir=None))
    job_id = store.list_jobs()[0]["job_id"]
    engine.cmd_map(store, NS(job=job_id, trunk=DEV_TRUNK))
    store.close()

    client = TestClient(engine_web.create_app(db_path=db, secret="test-secret"))
    dash = client.get("/")
    response = client.post(f"/run/{job_id}", data={"_csrf": _csrf(dash.text)})  # per-row Convert
    _wait_job(client, response)

    check = Store(db)
    assert check.get_job(job_id)["status"] == "dry_run"
    check.close()


def test_engine_web_favicon(tmp_path):
    import engine_web
    from fastapi.testclient import TestClient

    client = TestClient(engine_web.create_app(db_path=tmp_path / "dummy.db", secret="test-secret"))

    svg = client.get("/favicon.svg")
    assert svg.status_code == 200 and "svg" in svg.headers["content-type"]
    assert client.get("/favicon.ico").status_code == 200


def test_engine_web_flash_after_scan(tmp_path, monkeypatch):
    import engine_web
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "inbox").mkdir()
    db = tmp_path / "dummy.db"
    store = make_store(tmp_path)
    store.close()

    client = TestClient(engine_web.create_app(db_path=db, secret="test-secret"))
    dash = client.get("/")

    response = client.post("/scan", data={"_csrf": _csrf(dash.text)})
    _wait_job(client, response)

    page = client.get("/")
    assert "No new decks" in page.text


def test_unknown_layout_marks_needs_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "mystery.csv").write_text("Foo,Bar,Baz\n1,2,3\n", encoding="utf-8")
    store = make_store(tmp_path)

    engine.cmd_scan(store, NS(dir=None))
    job_id = store.list_jobs()[0]["job_id"]
    engine.cmd_map(store, NS(job=job_id, trunk=DEV_TRUNK))
    engine.cmd_run(store, NS(job=None))

    job = store.get_job(job_id)
    assert job["status"] == "needs_mapping"        # never silently converts empty rates
    assert "inter_rate" in job["notes"]
    store.close()
