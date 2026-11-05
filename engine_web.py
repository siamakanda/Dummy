"""Local dashboard for the folder-first engine (bound to 127.0.0.1).

No login - it is a localhost-only tool. CSRF protection is kept (an open page can still be cross-site
POSTed to localhost, and a push is a real portal write). Actions run in a worker thread so Playwright's
sync API works and the event loop stays responsive; failures are reported back on the page, not as a 500.
The UI never talks to the portal itself - the shared ``engine`` functions do.
"""
from __future__ import annotations

import contextlib
import hmac
import logging
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import config
import engine
from store import Store
from upload import commit_allowed

log = logging.getLogger("dummy.engine_web")
TEMPLATES_DIR = Path(__file__).parent / "templates"


def _open_store(db_path: str) -> Store:
    store = Store(db_path)
    if not store.all_trunks() and (config.DATA_DIR / "trunks.csv").exists():
        store.load_trunks(config.DATA_DIR / "trunks.csv")
    return store


def create_app(*, db_path=None, secret=None) -> FastAPI:
    db_path = str(db_path or config.DB_PATH)
    secret_key = secret or config.SESSION_SECRET or "dev-insecure-change-me"

    app = FastAPI(title="Dummy engine", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret_key, session_cookie="dummy_engine",
                       same_site="lax")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(24)
            request.session["csrf"] = token
        return token

    def csrf_ok(request: Request, form) -> bool:
        expected = request.session.get("csrf") or ""
        return bool(expected) and hmac.compare_digest(str(form.get("_csrf") or ""), expected)

    def render(request: Request, name: str, **context):
        return templates.TemplateResponse(
            request=request, name=name,
            context={"request": request, "csrf": csrf_token(request), **context})

    async def run_work(action) -> str:
        """Run a blocking action in a worker thread; return an error message, or '' on success."""
        try:
            await run_in_threadpool(action)
            return ""
        except Exception as exc:                       # keep the server up; tell the user instead
            log.exception("engine action failed")
            return str(exc)

    def back(error: str = "") -> RedirectResponse:
        return RedirectResponse("/" if not error else f"/?error={quote(error)}", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        error = request.query_params.get("error", "")
        store = _open_store(db_path)
        try:
            jobs = store.list_jobs()
            stats = {"total": len(jobs)}
            for job in jobs:
                stats[job["status"]] = stats.get(job["status"], 0) + 1
            trunks = store.all_trunks()
        finally:
            store.close()
        return render(request, "engine.html", jobs=jobs, trunks=trunks, stats=stats, error=error)

    @app.post("/map/{job_id}")
    async def map_job(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = _open_store(db_path)
            try:
                trunk_id = form.get("trunk_id")
                if store.get_job(job_id) and trunk_id:
                    store.set_job_mapping(job_id, int(trunk_id))
            finally:
                store.close()

        return back(await run_work(work))

    @app.post("/run")
    async def run_all(request: Request):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = _open_store(db_path)
            try:
                return engine.run_mapped(store)
            finally:
                store.close()

        return back(await run_work(work))

    @app.post("/scan")
    async def scan_inbox_route(request: Request):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = _open_store(db_path)
            try:
                return engine.scan_inbox(store)
            finally:
                store.close()

        return back(await run_work(work))

    @app.post("/push/{job_id}")
    async def push_job(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)
        commit = str(form.get("commit")) == "1"

        def work():
            # Own the SQLite connection inside this worker thread: Playwright's sync API needs a
            # thread with no running event loop, and sqlite3 connections are thread-affine.
            store = _open_store(db_path)
            try:
                job = store.get_job(job_id)
                if not job or not job["trunk_id"]:
                    return []
                allowed, _ = commit_allowed(store, store.get_trunk(int(job["trunk_id"])))
                portal_ctx = contextlib.nullcontext(None)
                if commit and allowed:                   # only open a browser when we will write
                    from portal import Portal
                    portal_ctx = Portal(headless=True)
                with portal_ctx as portal:
                    return engine.push_jobs(store, portal, commit=commit, job_id=job_id)
            finally:
                store.close()

        return back(await run_work(work))

    @app.get("/healthz")
    async def healthz():
        try:
            store = Store(db_path)
            store.conn.execute("SELECT 1").fetchone()
            unmapped = len(store.list_jobs("unmapped"))
            store.close()
        except Exception as exc:
            return PlainTextResponse(f"unhealthy: {exc}", status_code=503)
        return {"ok": True, "unmapped": unmapped}

    return app


app = create_app()
