"""Get the deck file out of an email: save attachments, unzip, follow download links (AGENTS.md M5).

Downloads use ``requests`` first, with a headless Playwright fallback for link pages that need JS.
Originals are written under the caller's archive dir and never overwritten.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import requests

from mailbox import Attachment, ParsedEmail
from retry import retry_call

DECK_SUFFIXES = (".csv", ".xlsx", ".xls")

def safe_filename(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._\- ]+", "_", Path(name or "").name).strip()
    return clean or "file"

def save_attachment(attachment: Attachment, dest_dir) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_filename(attachment.filename)
    dest.write_bytes(attachment.payload)
    return dest

def extract_zip(path, dest_dir) -> list[Path]:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            if (member.endswith("/") or "__MACOSX" in member
                    or Path(member).name.startswith("._")):      # skip macOS junk
                continue
            target = dest_dir / safe_filename(member)
            with archive.open(member) as source:
                target.write_bytes(source.read())
            extracted.append(target)
    return extracted

def _filename_from_response(url: str, response) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""", disposition)
    if match:
        return safe_filename(match.group(1))
    return safe_filename(Path(url.split("?")[0]).name or "download.bin")

def download_url(url: str, dest_dir, *, use_browser: bool = False, timeout: int = 30) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        response = retry_call(requests.get, url, timeout=timeout, allow_redirects=True,
                              exceptions=(requests.RequestException,), attempts=3, base_delay=0.3)
        response.raise_for_status()
        if use_browser and response.text[:400].lstrip().lower().startswith(("<html", "<!doctype")):
            raise ValueError(f"{url} returned an HTML page, not a file")
        dest = dest_dir / _filename_from_response(url, response)
        dest.write_bytes(response.content)
        return dest
    except Exception:
        if not use_browser:
            raise
        return download_with_browser(url, dest_dir)

def download_with_browser(url: str, dest_dir) -> Path:
    from playwright.sync_api import sync_playwright
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            with page.expect_download() as info:
                try:
                    page.goto(url)
                except Exception:
                    pass  # a download aborts the navigation
            download = info.value
            dest = dest_dir / safe_filename(download.suggested_filename or "download.bin")
            download.save_as(str(dest))
        finally:
            context.close()
            browser.close()
    return dest

def fetch_deck_files(parsed: ParsedEmail, dest_dir, *, use_browser: bool = False) -> list[Path]:
    """Save attachments (unzipping any archives) or follow download links; return deck files."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for attachment in parsed.attachments:
        path = save_attachment(attachment, dest_dir)
        if path.suffix.lower() == ".zip":
            files.extend(extract_zip(path, dest_dir / path.stem))
        else:
            files.append(path)

    if not any(f.suffix.lower() in DECK_SUFFIXES for f in files):
        for url in parsed.download_links():
            try:
                files.append(download_url(url, dest_dir, use_browser=use_browser))
            except Exception:
                continue

    return [f for f in files if f.suffix.lower() in DECK_SUFFIXES]
