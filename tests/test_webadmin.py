import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from store import Store
from web import create_app

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def make_client(tmp_path):
    app = create_app(db_path=tmp_path / "dummy.db", admin_user="admin",
                     admin_password="pw", secret="test-secret")
    return TestClient(app)


def seed_pending(tmp_path, **fields):
    store = Store(tmp_path / "dummy.db")
    store.load_trunks(FIXTURES / "trunks.csv")
    pending_id = store.add_pending("src1", kind="trunk", rows=2,
                                   effective_date="2026-12-02", reasons=["ambiguous"], **fields)
    store.close()
    return pending_id


def csrf_from(html: str) -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert match, "no csrf token in page"
    return match.group(1)


def login(client):
    token = csrf_from(client.get("/login").text)
    response = client.post("/login", data={"user": "admin", "password": "pw", "_csrf": token})
    assert response.status_code == 200


def test_unauthenticated_redirects_to_login(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/login"


def test_login_rejects_bad_password(tmp_path):
    client = make_client(tmp_path)
    token = csrf_from(client.get("/login").text)
    response = client.post("/login", data={"user": "admin", "password": "wrong", "_csrf": token})
    assert response.status_code == 200
    assert "Invalid credentials" in response.text


def test_dashboard_lists_pending(tmp_path):
    pending_id = seed_pending(tmp_path)
    client = make_client(tmp_path)
    login(client)
    response = client.get("/")
    assert "Pending items" in response.text
    assert str(pending_id) in response.text


def test_resolve_pending_sets_trunk(tmp_path):
    pending_id = seed_pending(tmp_path)
    client = make_client(tmp_path)
    login(client)
    token = csrf_from(client.get(f"/pending/{pending_id}").text)
    response = client.post(f"/pending/{pending_id}/resolve",
                           data={"_csrf": token, "action": "resolve", "trunk_id": "43"})
    assert response.status_code == 200
    store = Store(tmp_path / "dummy.db")
    item = store.get_pending(pending_id)
    assert item["status"] == "resolved"
    assert item["resolved_trunk_id"] == 43


def test_cancel_pending(tmp_path):
    pending_id = seed_pending(tmp_path)
    client = make_client(tmp_path)
    login(client)
    token = csrf_from(client.get(f"/pending/{pending_id}").text)
    client.post(f"/pending/{pending_id}/resolve", data={"_csrf": token, "action": "cancel"})
    store = Store(tmp_path / "dummy.db")
    assert store.get_pending(pending_id)["status"] == "cancelled"


def test_resolve_requires_csrf(tmp_path):
    pending_id = seed_pending(tmp_path)
    client = make_client(tmp_path)
    login(client)
    response = client.post(f"/pending/{pending_id}/resolve", data={"action": "cancel"})
    assert response.status_code == 403


def test_download_converted_csv(tmp_path):
    converted = tmp_path / "converted.csv"
    converted.write_text("prefix,destination\n1201200,NANPA\n", encoding="utf-8")
    pending_id = seed_pending(tmp_path, converted_path=converted, trunk_id=87)
    client = make_client(tmp_path)
    login(client)
    response = client.get(f"/pending/{pending_id}/download")
    assert response.status_code == 200
    assert "1201200" in response.text


def test_healthz_needs_no_auth(tmp_path):
    seed_pending(tmp_path)
    client = make_client(tmp_path)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["pending_open"] == 1
