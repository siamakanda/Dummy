"""Authenticated web admin over the same SQLite (M9 pending queue).

It only reads the DB and writes pending resolutions - it never touches the portal; the
``orchestrator.py --loop --commit`` daemon resumes resolved items and performs the portal write.
"""
from __future__ import annotations

import hmac
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import config
from store import Store

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(*, db_path=None, admin_user=None, admin_password=None, secret=None) -> FastAPI:
    db_path = str(db_path or config.DB_PATH)
    admin_user = config.ADMIN_USER if admin_user is None else admin_user
    admin_password = config.ADMIN_PASSWORD if admin_password is None else admin_password
    secret_key = secret or config.SESSION_SECRET or "dev-insecure-change-me"

    app = FastAPI(title="Dummy admin", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret_key, session_cookie="dummy_session",
                       same_site="lax", https_only=False)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(24)
            request.session["csrf"] = token
        return token

    def csrf_ok(request: Request, form) -> bool:
        expected = request.session.get("csrf") or ""
        given = str(form.get("_csrf") or "")
        return bool(expected) and hmac.compare_digest(given, expected)

    def render(request: Request, name: str, **context):
        return templates.TemplateResponse(
            request=request, name=name,
            context={"request": request, "csrf": csrf_token(request),
                     "user": request.session.get("user"), **context})

    def logged_in(request: Request) -> bool:
        return bool(request.session.get("user"))

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return render(request, "login.html", error="")

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)
        user, password = str(form.get("user", "")), str(form.get("password", ""))
        if (admin_password and hmac.compare_digest(user, admin_user)
                and hmac.compare_digest(password, admin_password)):
            request.session["user"] = user
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", error="Invalid credentials")

    @app.post("/logout")
    async def logout(request: Request):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        store = Store.open(db_path)
        try:
            pending = store.list_pending("open")
            for item in pending:
                trunk = store.get_trunk(item["trunk_id"]) if item.get("trunk_id") else None
                item["trunk_label"] = trunk["trunk_name"] if trunk else ""
            runs = store.list_runs(20)
            decks = store.list_decks(10)
        finally:
            store.close()
        return render(request, "dashboard.html", pending=pending, runs=runs, decks=decks)

    @app.get("/pending/{pid}", response_class=HTMLResponse)
    async def pending_detail(request: Request, pid: int):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        store = Store.open(db_path)
        try:
            item = store.get_pending(pid)
            if item is None:
                return PlainTextResponse("Not found", status_code=404)
            trunk = store.get_trunk(item["trunk_id"]) if item.get("trunk_id") else None
            reasons = json.loads(item.get("reasons") or "[]")
            trunks = store.all_trunks()
            head: list[str] = []
            converted = item.get("converted_path")
            if converted and Path(converted).exists():
                head = Path(converted).read_text(encoding="utf-8").splitlines()[:50]
        finally:
            store.close()
        return render(request, "pending.html", item=item, trunk=trunk, reasons=reasons,
                      trunks=trunks, head=head)

    @app.post("/pending/{pid}/resolve")
    async def pending_resolve(request: Request, pid: int):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)
        store = Store.open(db_path)
        try:
            if store.get_pending(pid) is None:
                return PlainTextResponse("Not found", status_code=404)
            if str(form.get("action")) == "cancel":
                store.set_pending_status(pid, "cancelled")
            else:
                trunk_raw = form.get("trunk_id")
                trunk_id = int(trunk_raw) if trunk_raw else None
                approve = form.get("approve") is not None
                store.resolve_pending(pid, trunk_id=trunk_id, approve=approve,
                                      by=request.session.get("user", "human"))
        finally:
            store.close()
        return RedirectResponse("/", status_code=303)

    @app.get("/pending/{pid}/download")
    async def pending_download(request: Request, pid: int):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        store = Store.open(db_path)
        try:
            item = store.get_pending(pid)
            converted = item.get("converted_path") if item else None
        finally:
            store.close()
        if not converted or not Path(converted).exists():
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(converted, filename=Path(converted).name)

    @app.get("/healthz")
    async def healthz():
        """Liveness/readiness for a reverse proxy or systemd (no auth)."""
        try:
            store = Store(db_path)
            store.conn.execute("SELECT 1").fetchone()
            open_count = len(store.list_pending("open"))
            store.close()
        except Exception as exc:
            return PlainTextResponse(f"unhealthy: {exc}", status_code=503)
        return {"ok": True, "pending_open": open_count}

    return app


app = create_app()
