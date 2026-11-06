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


def test_upload_refuses_unapproved_trunk(tmp_path):
    store = make_store(tmp_path)
    job_id = store.add_job("file:z", file_path="z.csv", file_name="z.csv")
    store.set_job_mapping(job_id, 82)                        # DW USA CONVO TRUNK 6060 - not dev/approved
    store.update_job(job_id, status="dry_run")

    results = engine.upload_jobs(store, object(), commit=True)  # portal unused: refused first

    assert results[0]["status"] == "refused"
    assert store.get_job(job_id)["status"] == "dry_run"


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


def test_engine_web_upload_refuses_unapproved_trunk(tmp_path):
    """Upload must not launch a browser for a trunk it will refuse."""
    import engine_web
    from fastapi.testclient import TestClient

    db = tmp_path / "dummy.db"
    store = make_store(tmp_path)
    job_id = store.add_job("file:r", file_path="r.csv", file_name="r.csv")
    store.set_job_mapping(job_id, 82)                        # DW USA CONVO TRUNK 6060 - not dev/approved
    store.update_job(job_id, status="dry_run")
    store.close()

    client = TestClient(engine_web.create_app(db_path=db, secret="test-secret"))
    dash = client.get("/")
    assert "r.csv" in dash.text

    response = client.post(f"/upload/{job_id}", data={"_csrf": _csrf(dash.text)})
    _wait_job(client, response)

    check = Store(db)
    assert check.get_job(job_id)["status"] == "dry_run"      # refused: nothing written
    check.close()


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
