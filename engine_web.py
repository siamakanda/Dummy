"""Local dashboard for the folder-first engine (bound to 127.0.0.1).

No login - it is a localhost-only tool. CSRF protection is kept (an open page can still be cross-site
POSTed to localhost, and a push is a real portal write).

Actions run in a **background thread** and return a job id immediately, so the page never sits on a
blocking load. The dashboard polls ``/jobs/<id>`` and streams the engine's log lines into a live console
overlay; when the job ends it reloads and shows the resulting success/info/warning/error banner.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
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


class _BufferHandler(logging.Handler):
    """Collects log lines from the running job into its console buffer."""

    def __init__(self, lines, lock):
        super().__init__()
        self._lines = lines
        self._lock = lock
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            text = self.format(record)
        except Exception:                                  # never let logging break a job
            text = record.getMessage()
        with self._lock:
            self._lines.append(text)


class _JobManager:
    """Tiny in-memory job + flash store for a single-user local dashboard."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[int, dict] = {}
        self._counter = 0
        self._flashes: list[dict] = []

    def start(self, kind: str, action) -> int:
        with self._lock:
            self._counter += 1
            job_id = self._counter
            job = {"id": job_id, "kind": kind, "status": "running", "lines": [],
                   "started": time.time()}
            self._jobs[job_id] = job
            finished = [i for i, j in self._jobs.items() if j["status"] != "running"]
            for old in finished[:-20]:                     # keep memory bounded
                self._jobs.pop(old, None)

        def run():
            handler = _BufferHandler(job["lines"], self._lock)
            logger = logging.getLogger("dummy")
            logger.addHandler(handler)
            with self._lock:
                job["lines"].append(f"$ {kind}")
            try:
                action()
                job["status"] = "done"
            except Exception as exc:
                log.exception("job %s failed", job_id)
                with self._lock:
                    job["lines"].append(f"ERROR: {exc}")
                self.flash("error", "Something went wrong",
                           "The action could not be completed. Please try again.")
                job["status"] = "error"
            finally:
                logger.removeHandler(handler)
                job["finished"] = time.time()

        threading.Thread(target=run, daemon=True, name=f"dummy-job-{job_id}").start()
        return job_id

    def get(self, job_id: int) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {"id": job["id"], "kind": job["kind"], "status": job["status"],
                    "lines": list(job["lines"]),
                    "elapsed": round((job.get("finished") or time.time()) - job["started"], 1)}

    def flash(self, kind: str, title: str, message: str = "") -> None:
        with self._lock:
            self._flashes.append({"kind": kind, "title": title, "message": message})

    def pop_flashes(self) -> list[dict]:
        with self._lock:
            items = self._flashes
            self._flashes = []
            return items


def create_app(*, db_path=None, secret=None) -> FastAPI:
    db_path = str(db_path or config.DB_PATH)
    secret_key = secret or config.SESSION_SECRET or "dev-insecure-change-me"

    app = FastAPI(title="Dummy engine", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret_key, session_cookie="dummy_engine",
                       same_site="lax")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    jobs = _JobManager()

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

    def render(request: Request, name: str, **context):
        return templates.TemplateResponse(
            request=request, name=name,
            context={"request": request, "csrf": csrf_token(request), **context})

    # ---- routes ----------------------------------------------------------
    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(content=FAVICON, media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        flashes = jobs.pop_flashes()
        store = Store.open(db_path)
        try:
            all_jobs = store.list_jobs()
            stats = {"total": len(all_jobs)}
            for job in all_jobs:
                stats[job["status"]] = stats.get(job["status"], 0) + 1
                trunk = store.get_trunk(job["trunk_id"]) if job["trunk_id"] else None
                job["trunk_label"] = trunk["trunk_name"] if trunk else ""
                job["can_write"] = bool(trunk) and commit_allowed(store, trunk)[0]
            trunks = store.all_trunks()
        finally:
            store.close()
        return render(request, "engine.html", jobs=all_jobs, trunks=trunks, stats=stats,
                      flashes=flashes)

    @app.get("/jobs/{job_id}")
    async def job_status(job_id: int):
        job = jobs.get(job_id)
        if job is None:
            return PlainTextResponse("unknown job", status_code=404)
        return job

    @app.post("/map/{job_id}")
    async def map_job(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = Store.open(db_path)
            try:
                trunk_id = form.get("trunk_id")
                job = store.get_job(job_id)
                if not job or not trunk_id:
                    jobs.flash("warning", "No trunk selected",
                               "Choose a trunk from the list before saving.")
                    return
                store.set_job_mapping(job_id, int(trunk_id))
                trunk = store.get_trunk(int(trunk_id))
                name = trunk["trunk_name"] if trunk else f"trunk {trunk_id}"
                jobs.flash("success", "Mapping saved", f"{job['file_name']} is now mapped to {name}.")
            finally:
                store.close()

        return {"job": jobs.start("Save mapping", work)}

    @app.post("/run")
    async def run_all(request: Request):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = Store.open(db_path)
            try:
                results = engine.run_mapped(store)
            finally:
                store.close()
            if not results:
                jobs.flash("info", "Nothing to convert",
                           "Map at least one deck to a trunk, then convert again.")
                return
            ready = sum(1 for item in results if item.get("status") == "dry_run")
            issues = len(results) - ready
            if issues:
                jobs.flash("warning", "Converted with issues",
                           f"{ready} deck(s) are ready to review; {issues} need attention - "
                           "see the status column.")
            else:
                jobs.flash("success", "Conversion complete",
                           f"{ready} deck(s) converted to our template and ready to review.")

        return {"job": jobs.start("Convert mapped decks", work)}

    @app.post("/run/{job_id}")
    async def run_one(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = Store.open(db_path)
            try:
                results = engine.run_mapped(store, job_id)
            finally:
                store.close()
            first = (results or [{}])[0]
            status = first.get("status")
            if status == "dry_run":
                jobs.flash("success", "Deck converted",
                           "The deck is ready to review. Push it when you're satisfied.")
            elif status == "validation_error":
                jobs.flash("warning", "Deck needs attention",
                           "It didn't pass validation - open the status details before pushing.")
            elif status == "needs_mapping":
                jobs.flash("warning", "Unknown layout",
                           "Some columns couldn't be mapped. Set the trunk's header aliases, "
                           "then convert again.")
            else:
                jobs.flash("info", "Nothing to convert", "Map this deck to a trunk first.")

        return {"job": jobs.start("Convert deck", work)}

    @app.post("/scan")
    async def scan_inbox(request: Request):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = Store.open(db_path)
            try:
                scanned, created = engine.scan_inbox(store)
            finally:
                store.close()
            if created:
                jobs.flash("success", "Scan complete",
                           f"{scanned} file(s) scanned, {created} new deck(s) added to the inbox.")
            else:
                jobs.flash("info", "No new decks",
                           f"{scanned} file(s) scanned - nothing new to add.")

        return {"job": jobs.start("Scan inbox", work)}

    @app.post("/upload/{job_id}")
    async def upload_job_route(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            # Own the SQLite connection inside this worker thread: Playwright's sync API needs a
            # thread with no running event loop, and sqlite3 connections are thread-affine.
            store = Store.open(db_path)
            try:
                job = store.get_job(job_id)
                if not job or not job["trunk_id"]:
                    jobs.flash("info", "Nothing to upload", "Convert a mapped deck first.")
                    return
                from portal import Portal
                with Portal(headless=True) as portal:
                    results = engine.upload_jobs(store, portal, commit=True, job_id=job_id)
                item = (results or [{}])[0]
                status = item.get("status")
                if status == "uploaded":
                    jobs.flash("success", "Deck uploaded",
                               f"{item.get('deck_name', 'The deck')} was created and its rates uploaded "
                               f"(deck {item.get('deck_id')}). Assign it to the trunk next.")
                elif status == "refused":
                    jobs.flash("warning", "Upload blocked",
                               item.get("reason") or "This trunk isn't approved.")
                else:
                    jobs.flash("error", "Upload failed",
                               item.get("error") or "The deck could not be uploaded.")
            finally:
                store.close()

        return {"job": jobs.start("Upload to deck", work)}

    @app.post("/assign/{job_id}")
    async def assign_job_route(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = Store.open(db_path)
            try:
                job = store.get_job(job_id)
                if not job or not job.get("deck_id"):
                    jobs.flash("info", "Nothing to assign",
                               "Upload the deck to the switch first.")
                    return
                allowed, _ = commit_allowed(store, store.get_trunk(int(job["trunk_id"])))
                if not allowed:
                    jobs.flash("warning", "Assign blocked",
                               "This trunk isn't approved for automatic writes.")
                    return
                from portal import Portal
                with Portal(headless=True) as portal:
                    results = engine.assign_jobs(store, portal, commit=True, job_id=job_id)
                item = (results or [{}])[0]
                if item.get("status") == "assigned":
                    jobs.flash("success", "Deck assigned",
                               f"{item.get('deck_name', 'The deck')} is now the rate deck on the trunk.")
                elif item.get("status") == "refused":
                    jobs.flash("warning", "Assign blocked",
                               item.get("reason") or "This trunk isn't approved for automatic writes.")
                else:
                    jobs.flash("error", "Assign failed",
                               item.get("error") or "The deck could not be assigned. It is still uploaded.")
            finally:
                store.close()

        return {"job": jobs.start("Assign to trunk", work)}

    @app.post("/approve/{job_id}")
    async def approve_trunk_route(request: Request, job_id: int):
        form = await request.form()
        if not csrf_ok(request, form):
            return PlainTextResponse("bad csrf", status_code=403)

        def work():
            store = Store.open(db_path)
            try:
                job = store.get_job(job_id)
                trunk = store.get_trunk(int(job["trunk_id"])) if job and job.get("trunk_id") else None
                if not trunk:
                    jobs.flash("info", "Nothing to approve", "Map the deck to a trunk first.")
                    return
                store.approve(int(trunk["trunk_id"]), by="dashboard",
                              carrier_id=trunk.get("carrier_id"))
                jobs.flash("success", "Trunk approved",
                           f"{trunk['trunk_name']} may now receive uploaded decks. Assign when ready.")
            finally:
                store.close()

        return {"job": jobs.start("Approve trunk", work)}

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
