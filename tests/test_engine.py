"""Tests for the folder-first engine (offline; no portal, no browser)."""
import pathlib
import re
import sys
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

    engine.cmd_push(store, NS(job=None, commit=False, headed=False))     # dry-run: no write
    assert store.get_job(job_id)["status"] == "dry_run"


def test_push_refuses_unapproved_trunk(tmp_path):
    store = make_store(tmp_path)
    job_id = store.add_job("file:z", file_path="z.csv", file_name="z.csv")
    store.set_job_mapping(job_id, 82)                        # DW USA CONVO TRUNK 6060 - not dev/approved
    store.update_job(job_id, status="dry_run")

    results = engine.push_jobs(store, object(), commit=True)  # portal unused: refused first

    assert results[0]["status"] == "refused"
    assert store.get_job(job_id)["status"] == "dry_run"


def _csrf(html: str) -> str:
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


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


def test_commit_job_creates_uploads_and_assigns(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    portal = FakePortal()

    results = engine.push_jobs(store, portal, commit=True, job_id=job_id)

    assert results[0]["status"] == "uploaded"
    assert portal.created and portal.uploaded and portal.assigned      # create + upload + assign
    assert portal.assigned[0][2] == 999                                # the new deck id
    assert portal.deleted == []
    assert store.get_job(job_id)["status"] == "uploaded"


def test_commit_job_cleans_up_orphan_on_assign_failure(tmp_path, monkeypatch):
    store, job_id = _mapped_job(tmp_path, monkeypatch)
    portal = FakePortal(assign_ok=False)

    results = engine.push_jobs(store, portal, commit=True, job_id=job_id)

    assert results[0]["status"] == "failed"
    assert portal.deleted == [999]                                     # orphan removed
    assert store.get_job(job_id)["status"] == "failed"


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

    client.post(f"/map/{job_id}", data={"_csrf": _csrf(dash.text), "trunk_id": "87"})

    check = Store(db)
    assert check.get_job(job_id)["trunk_id"] == 87
    check.close()


def test_engine_web_push_commit_refuses_unapproved_trunk(tmp_path):
    """The dashboard push must not launch a browser for a trunk it is going to refuse."""
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

    response = client.post(f"/push/{job_id}",
                           data={"_csrf": _csrf(dash.text), "commit": "1"},
                           follow_redirects=False)

    assert response.status_code == 303
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
    client.post(f"/run/{job_id}", data={"_csrf": _csrf(dash.text)})   # the per-row Convert button

    check = Store(db)
    assert check.get_job(job_id)["status"] == "dry_run"
    check.close()
