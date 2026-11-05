from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext, Error as PlaywrightError
import config
from parse import extract_decks, extract_trunks

STATE_FILE = config.STATE_DIR / "state.json"

class Portal:
    """Thin wrapper around the SBC switch portal. All portal-specific knowledge lives here."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = self._browser = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self):
        config.require_credentials()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        kw = {"storage_state": str(STATE_FILE)} if STATE_FILE.exists() else {}
        self.ctx = self._browser.new_context(accept_downloads=True, **kw)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(30_000)
        self.ensure_logged_in()
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close(); self._browser.close()
        finally:
            self._pw.stop()

    # ---- auth -------------------------------------------------------------
    def _on_login_page(self) -> bool:
        return "/login" in self.page.url or self.page.locator("input[type=password]").count() > 0

    def ensure_logged_in(self):
        self.page.goto(f"{config.BASE_URL}/rate_decks")
        self.page.wait_for_load_state("networkidle")
        if not self._on_login_page():
            return
        self.page.goto(f"{config.BASE_URL}/login")
        self.page.wait_for_load_state("networkidle")
        pw = self.page.locator("input[type=password]").first
        user = self.page.locator(
            "input[name=username], input[name=user], input[name=email], "
            "input[type=text]:visible, input[type=email]:visible").first
        user.fill(config.USERNAME)
        pw.fill(config.PASSWORD)
        pw.press("Enter")
        self.page.wait_for_load_state("networkidle")
        if self._on_login_page():
            self.snapshot("login_failed")
            raise RuntimeError("Login failed (check .env credentials, or CAPTCHA/2FA). See data/snapshots/login_failed.*")
        self.ctx.storage_state(path=str(STATE_FILE))

    # ---- helpers ----------------------------------------------------------
    def goto(self, path: str):
        self.page.goto(f"{config.BASE_URL}/{path.lstrip('/')}")
        self.page.wait_for_load_state("networkidle")
        if self._on_login_page():          # session expired mid-run
            self.ensure_logged_in()
            self.page.goto(f"{config.BASE_URL}/{path.lstrip('/')}")
            self.page.wait_for_load_state("networkidle")

    def show_all_rows(self):
        """If the table is a paginated DataTable, switch page length to the largest option."""
        sel = self.page.locator("select[name$='_length']")
        if sel.count():
            opts = sel.first.locator("option").evaluate_all("els => els.map(e => e.value)")
            nums = [(int(o) if o.lstrip('-').isdigit() else 0, o) for o in opts]
            best = next((o for n, o in nums if n == -1), None) or max(nums)[1]
            sel.first.select_option(best)
            self.page.wait_for_timeout(500)

    def snapshot(self, name: str):
        (config.SNAP_DIR / f"{name}.html").write_text(self.page.content(), encoding="utf-8")
        self.page.screenshot(path=str(config.SNAP_DIR / f"{name}.png"), full_page=True)

    # ---- portal operations ------------------------------------------------
    def carriers_html(self) -> str:
        """The /carriers page — boxes link to each carrier's trunk page (/carrier_trunks.php?id=N)."""
        self.goto("carriers.php")
        self.show_all_rows()
        return self.page.content()

    def carrier_trunks_html(self, carrier_id: int) -> str:
        self.goto(f"carrier_trunks.php?id={carrier_id}")
        self.show_all_rows()
        return self.page.content()

    def download(self, url_path: str, dest_dir=None) -> Path:
        """Save a URL that responds with a file (not a page), e.g. the rate-deck template."""
        dest_dir = Path(dest_dir) if dest_dir else config.DATA_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        url = f"{config.BASE_URL}/{url_path.lstrip('/')}"
        with self.page.expect_download() as info:
            try:
                self.page.goto(url)
            except PlaywrightError as err:      # a download aborts the navigation
                if "Download is starting" not in str(err):
                    raise
        download = info.value
        dest = dest_dir / (download.suggested_filename or "download.bin")
        download.save_as(str(dest))
        return dest

    def download_template(self, dest_dir=None) -> Path:
        """The portal's rate-deck template CSV (used to know the exact upload format)."""
        return self.download("rate_deck_upload.php?id=37&template=1", dest_dir)

    # ---- writers (dry-run is enforced by the caller / upload.py) ----------
    def _set_checkbox(self, selector: str, checked: bool) -> None:
        locator = self.page.locator(selector)
        if locator.count() == 0:
            return
        if checked:
            locator.first.check(force=True)
        else:
            locator.first.uncheck(force=True)

    def find_deck_id(self, name: str) -> int | None:
        """Return the newest deck id whose name matches (max id wins on duplicates)."""
        self.goto("rate_decks.php")
        target = name.strip()
        matches = [deck["deck_id"] for deck in extract_decks(self.page.content())
                   if deck["name"].strip() == target]
        return max(matches) if matches else None

    def list_decks(self) -> list[dict]:
        """All deck cards on /rate_decks (id, name, type, mode, status)."""
        self.goto("rate_decks.php")
        return extract_decks(self.page.content())

    def deck_is_assigned(self, deck_id: int) -> bool:
        """True if the deck is assigned to any trunk (fail-safe: errors count as assigned)."""
        try:
            response = self.ctx.request.get(f"{config.BASE_URL}/rate_decks.php?assignments={deck_id}")
            data = response.json()
        except Exception:
            return True
        return bool(data)

    def existing_deck_id(self, carrier_id: int, trunk_id: int) -> int | None:
        """The deck currently wired to this trunk (for `--reuse-deck`)."""
        name = self.trunk_deck(carrier_id, trunk_id)
        return self.find_deck_id(name) if name else None

    def create_rate_deck(self, name: str, *, description: str = "", lrn_billing: bool = True,
                         deck_type: str = "carrier", currency: str = "USD",
                         mode: str = "npanxx", enabled: bool = True) -> int:
        """Create a rate deck on /rate_decks and return its new id (found by name)."""
        self.goto("rate_decks.php")
        self.page.eval_on_selector("#addForm", "el => el.classList.add('show')")  # reveal the form
        form = self.page.locator("#addForm form")
        form.locator("input[name=name]").fill(name)
        form.locator("select[name=type]").select_option(deck_type)
        form.locator("select[name=currency]").select_option(currency)
        form.locator("select[name=mode]").select_option(mode)
        if description:
            form.locator("input[name=description]").fill(description)
        self._set_checkbox("#addForm input[name=enabled]", enabled)
        self._set_checkbox("#lrn_billing", lrn_billing)          # "Use LRN for billing"
        self.snapshot("create_deck_before")
        form.locator("button[type=submit]").click()
        self.page.wait_for_load_state("networkidle")
        self.snapshot("create_deck_after")
        deck_id = self.find_deck_id(name)
        if deck_id is None:
            raise RuntimeError(
                f"deck '{name}' was submitted but not found in the list - check "
                f"data/snapshots/create_deck_after.*")
        return deck_id

    def upload_rate_deck(self, deck_id: int, csv_path, *, effective_date: str | None = None,
                         replace: bool = True) -> dict:
        """Upload a converted CSV to an existing deck; returns {ok, url, deck_id, screenshot}."""
        self.goto(f"rate_deck_upload.php?id={deck_id}")
        self.page.set_input_files("input#csvFile", str(csv_path))
        if effective_date:
            self.page.fill("input[name=effective_date]", effective_date)
        self._set_checkbox("#replaceMode", replace)
        self.snapshot(f"upload_before_{deck_id}")
        self.page.click("#uploadForm button[type=submit]")
        self.page.wait_for_load_state("networkidle")
        self.snapshot(f"upload_result_{deck_id}")

        text = self.page.inner_text("body").lower()
        success = any(word in text for word in ("imported", "success", "uploaded", "inserted"))
        failed = any(word in text for word in ("error", "invalid", "failed", "not allowed"))
        return {
            "ok": bool(success and not failed),
            "url": self.page.url,
            "deck_id": deck_id,
            "screenshot": str(config.SNAP_DIR / f"upload_result_{deck_id}.png"),
        }

    def trunk_deck(self, carrier_id: int, trunk_id: int) -> str:
        """The trunk's current cost-deck name (read from the carrier page)."""
        self.goto(f"carrier_trunks.php?id={carrier_id}")
        self.show_all_rows()
        for trunk in extract_trunks(self.page.content()):
            if str(trunk["trunk_id"]) == str(trunk_id):
                return str(trunk.get("cost_deck") or "")
        return ""

    def _open_routing_tab(self) -> None:
        """Reveal the Rate Deck selector: it lives under the trunk editor's 'Routing' tab."""
        tab = self.page.locator("button:has-text('Routing'), a:has-text('Routing')").first
        if tab.count():
            try:
                tab.click(timeout=5000)
                self.page.wait_for_timeout(400)
            except PlaywrightError:
                pass

    def _select_rate_deck(self, deck_id: int) -> None:
        select = self.page.locator("select[name=rate_deck_id]").first
        select.scroll_into_view_if_needed()
        select.select_option(str(deck_id))

    def assign_deck_to_trunk(self, carrier_id: int, trunk_id: int, deck_id: int,
                             *, expect_name: str | None = None) -> dict:
        """Assign a deck to a trunk (Routing tab -> `rate_deck_id` -> Update Trunk) and verify."""
        self.goto(f"carrier_trunks.php?id={carrier_id}&edit={trunk_id}")
        self._open_routing_tab()
        try:
            self._select_rate_deck(deck_id)
        except PlaywrightError:
            self.goto(f"carrier_trunks.php?id={carrier_id}&edit={trunk_id}")  # retry once
            self._open_routing_tab()
            self._select_rate_deck(deck_id)
        self.snapshot(f"assign_before_{trunk_id}")
        self.page.locator("form:has(select[name=rate_deck_id]) button:has-text('Update Trunk')").first.click()
        self.page.wait_for_load_state("networkidle")
        self.snapshot(f"assign_after_{trunk_id}")
        current = self.trunk_deck(carrier_id, trunk_id)
        ok = (not expect_name) or (expect_name.strip().lower() in current.strip().lower())
        return {"ok": ok, "cost_deck": current}

    def delete_rate_deck(self, deck_id: int) -> bool:
        """Delete a just-created deck (orphan cleanup). True if the delete form was submitted."""
        self.goto("rate_decks.php")
        form = self.page.locator(
            f"form[action='/actions/rate_deck_delete.php']:has(input[name=id][value='{deck_id}'])").first
        if form.count() == 0:
            return False
        self.page.once("dialog", lambda dialog: dialog.accept())
        form.locator("button[type=submit]").first.click()
        self.page.wait_for_load_state("networkidle")
        return True
