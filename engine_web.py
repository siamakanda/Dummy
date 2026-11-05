"""Local dashboard for the folder-first engine (bound to 127.0.0.1).

No login - it is a localhost-only tool. CSRF protection is kept (an open page can still be cross-site
POSTed to localhost, and a push is a real portal write). Actions run in a worker thread so Playwright's
sync API works and the event loop stays responsive. Every action ends with a human, on-page message
(success / info / warning / error) instead of a raw exception or a 500.
"""
from __future__ import annotations

import contextlib
import hmac
import logging
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import config
import engine
from store import Store
from upload import commit_allowed

log = logging.getLogger("dummy.engine_web")
TEMPLATES_DIR = Path(__file__).parent / "templates"

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#6366f1"/><stop offset="0.55" stop-color="#4f46e5"/>'
    '<stop offset="1" stop-color="#4338ca"/></linearGradient></defs>'
    '<rect width="64" height="64" rx="16" fill="url(#g)"/>'
    '<g fill="none" stroke="#ffffff" stroke-width="5" stroke-linecap="round">'
    '<path d="M18 23h28"/><path d="M18 32h18"/><path d="M18 41h12"/></g></svg>'
)


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

    # ---- session helpers -------------------------------------------------
    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(24)
            request.session["csrf"] = token
        return token

    def csrf_ok(request: Request, form) -> bool:
        expected = request.session.get("csrf") or ""
        return bool(expected) and hmac.compare_digest(str(form.get("_csrf") or ""), expected)

    def flash(request: Request, kind: str, title: str, message: str = "") -> None:
        """Queue a one-time, human-readable message for the next page render."""
        items = request.session.get("flash") or []
        items.append({"kind": kind, "title": title, "message": message})
        request.session["flash"] = items

    def pop_flashes(request: Request) -> list[dict]:
        flashes = request.session.pop("flash", None) or []
        request.session["flash"] = []          # keep the key present so the cookie round-trips
        return flashes

    def render(request: Request, name: str, **context):
        return templates.TemplateResponse(
            request=request, name=name,
            context={"request": request, "csrf": csrf_token(request), **context})

    async def run_action(action):
        """Run a blocking action in a worker thread -> (result, error_message)."""
        try:
            return await run_in_threadpool(action), ""
        except Exception as exc:                       # keep the server up; report it on the page
            log.exception("engine action failed")
            return None, str(exc)

    def back() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    # ---- routes ----------------------------------------------------------
    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(content=FAVICON, media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        flashes = pop_flashes(request)
        store = _open_store(db_path)
        try:
            jobs = store.list_jobs()
            stats = {"total": len(jobs)}
            for job in jobs:
                stats[job["status"]] = stats.get(job["status"], 0) + 1
                trunk = store.get_trunk(job["trunk_id"]) if job["trunk_id"] else None
                job["trunk_label"] = trunk["trunk_name"] if trunk else ""
            trunks = store.all_trunks()
        finally:
            store.close()
        return render(request, "engine.html", jobs=jobs, trunks=trunks, stats=stats, flashes=flashes)

    @app.post("/map/{job_id}")
    async def map_job(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = _open_store(db_path)
            try:
                trunk_id = form.get("trunk_id")
                job = store.get_job(job_id)
                if not job or not trunk_id:
                    return None
                store.set_job_mapping(job_id, int(trunk_id))
                trunk = store.get_trunk(int(trunk_id))
                return {"file": job["file_name"],
                        "trunk": trunk["trunk_name"] if trunk else f"trunk {trunk_id}"}
            finally:
                store.close()

        result, error = await run_action(work)
        if error:
            flash(request, "error", "Couldn't save the mapping",
                  "The deck was not updated. Please try again.")
        elif result:
            flash(request, "success", "Mapping saved",
                  f"{result['file']} is now mapped to {result['trunk']}.")
        else:
            flash(request, "warning", "No trunk selected",
                  "Choose a trunk from the list before saving.")
        return back()

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

        result, error = await run_action(work)
        if error:
            flash(request, "error", "Conversion failed",
                  "The decks could not be converted. Please try again.")
            return back()
        results = result or []
        if not results:
            flash(request, "info", "Nothing to convert",
                  "Map at least one deck to a trunk, then convert again.")
            return back()
        ready = sum(1 for item in results if item.get("status") == "dry_run")
        issues = len(results) - ready
        if issues:
            flash(request, "warning", "Converted with issues",
                  f"{ready} deck(s) are ready to review; {issues} need attention - see the status column.")
        else:
            flash(request, "success", "Conversion complete",
                  f"{ready} deck(s) converted to our template and ready to review.")
        return back()

    @app.post("/run/{job_id}")
    async def run_one(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = _open_store(db_path)
            try:
                return engine.run_mapped(store, job_id)
            finally:
                store.close()

        result, error = await run_action(work)
        if error:
            flash(request, "error", "Conversion failed",
                  "This deck could not be converted. Please try again.")
            return back()
        first = (result or [{}])[0]
        status = first.get("status")
        if status == "dry_run":
            flash(request, "success", "Deck converted",
                  "The deck is ready to review. Push it when you're satisfied.")
        elif status == "validation_error":
            flash(request, "warning", "Deck needs attention",
                  "It didn't pass validation - open the status details before pushing.")
        else:
            flash(request, "info", "Nothing to convert",
                  "Map this deck to a trunk first.")
        return back()

    @app.post("/scan")
    async def scan_inbox(request: Request):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = _open_store(db_path)
            try:
                return engine.scan_inbox(store)
            finally:
                store.close()

        result, error = await run_action(work)
        if error:
            flash(request, "error", "Scan failed",
                  "The inbox folder could not be scanned. Please try again.")
            return back()
        scanned, created = result or (0, 0)
        if created:
            flash(request, "success", "Scan complete",
                  f"{scanned} file(s) scanned, {created} new deck(s) added to the inbox.")
        else:
            flash(request, "info", "No new decks",
                  f"{scanned} file(s) scanned - nothing new to add.")
        return back()

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

        result, error = await run_action(work)
        if error:
            flash(request, "error", "Push failed",
                  "The deck could not be pushed to the switch. Nothing was changed.")
            return back()
        item = (result or [{}])[0]
        status = item.get("status")
        if status == "uploaded":
            flash(request, "success", "Deck pushed",
                  f"{item.get('deck_name', 'The deck')} was created, uploaded and assigned to the trunk.")
        elif status == "dry_run":
            flash(request, "info", "Dry run complete",
                  "Nothing was written to the switch. Push when you're ready.")
        elif status == "refused":
            flash(request, "warning", "Push blocked",
                  item.get("reason") or "This trunk isn't approved for automatic writes.")
        elif status == "failed":
            flash(request, "error", "Push failed",
                  item.get("error") or "The deck could not be pushed to the switch.")
        else:
            flash(request, "info", "Nothing to push",
                  "Convert a mapped deck before pushing.")
        return back()

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
