"""Pluggable input sources for Dummy (AGENTS M8).

The pipeline can be fed from **email (IMAP)** or a **local directory** of rate-deck files. Each
`Source.fetch()` returns `InputItem`s that the orchestrator processes identically afterwards.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from cleaner import read_clean
from email_llm import EmailFacts, extract_facts
from fetcher import extract_zip, fetch_deck_files
from mailbox import parse_email
from mapper import parse_effective_date, resolve_columns

DECK_PATTERNS = (".csv", ".xlsx", ".xls", ".zip")

log = logging.getLogger("dummy.input")

@dataclass
class InputItem:
    source_id: str                    # idempotency key: Message-ID or "file:<sha1>"
    work_dir: Path                    # archive dir for this item (files already materialized)
    deck_files: list[Path]            # csv/xlsx the orchestrator will convert
    facts: EmailFacts                 # carrier / trunk / effective-date hints
    raw: bytes | None = None          # original bytes (email or file) for the archive
    origin: str = "email"             # "email" | "directory"
    meta: dict = field(default_factory=dict)

def safe_name(name: str, limit: int = 80) -> str:
    """Filesystem-safe, length-bounded name (huge/odd Message-IDs must not break the path)."""
    cleaned = re.sub(r"[^A-Za-z0-9._@-]+", "_", (name or "").strip()).strip("._- ") or "item"
    if len(cleaned) <= limit:
        return cleaned
    digest = hashlib.sha1((name or "").encode("utf-8", "replace")).hexdigest()[:10]
    return f"{cleaned[:limit - 11]}-{digest}"

class EmailSource:
    """Wraps an open IMAP `Mailbox`: UNSEEN -> parsed email -> facts + deck files."""

    def __init__(self, mailbox, *, use_llm: bool | None = None, limit: int | None = None):
        self.mailbox = mailbox
        self.use_llm = use_llm
        self.limit = limit

    def fetch(self, work_root: Path, store=None, limit: int | None = None) -> list[InputItem]:
        raws = self.mailbox.fetch_raw("UNSEEN")
        cap = limit if limit is not None else self.limit
        if cap:
            raws = raws[-cap:]                                  # newest N
        log.info("email source: %d message(s)%s", len(raws), f" (limit {cap})" if cap else "")
        items = []
        for index, raw in enumerate(raws, 1):
            parsed = parse_email(raw)
            source_id = parsed.message_id or ("sha1:" + hashlib.sha1(raw).hexdigest())
            if store is not None and store.source_uploaded(source_id):
                continue
            log.info("  [%d/%d] %s", index, len(raws), (parsed.subject or source_id)[:70])
            work_dir = Path(work_root) / safe_name(source_id)
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "message.eml").write_bytes(raw)
            facts = extract_facts(parsed, use_llm=self.use_llm)
            files = fetch_deck_files(parsed, work_dir)
            items.append(InputItem(source_id, work_dir, files, facts, raw, "email",
                                   {"subject": parsed.subject, "sender": parsed.sender}))
        return items

class DirectorySource:
    """Reads rate-deck files dropped in a local folder (csv/xlsx/zip)."""

    def __init__(self, root: str | Path, *, trunk_override: str | None = None, limit: int | None = None):
        self.root = Path(root)
        self.trunk_override = trunk_override
        self.limit = limit

    def _paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            (p for p in self.root.iterdir()
             if p.is_file() and p.suffix.lower() in DECK_PATTERNS
             and not p.name.endswith(".json")),
            key=lambda p: p.stat().st_mtime, reverse=True)

    def _sidecar(self, path: Path) -> dict:
        for candidate in (Path(str(path) + ".json"), path.with_suffix(path.suffix + ".json")):
            if candidate.exists():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8")) or {}
                except Exception:
                    return {}
        return {}

    def _facts(self, path: Path) -> EmailFacts:
        data = self._sidecar(path)
        effective = (parse_effective_date(str(data.get("effective_date", "")).strip())
                     or self._effective_from_file(path))
        return EmailFacts(
            carrier_name=str(data.get("carrier_name", "")),
            trunk_name=str(data.get("trunk_name", "")),
            tech_prefix=str(data.get("tech_prefix", "")),
            product=str(data.get("product", "")),
            effective_date=effective,
            deck_filename=path.name,
        )

    @staticmethod
    def _effective_from_file(path: Path) -> str:
        try:
            frame = read_clean(path)
        except Exception:
            return ""
        index = resolve_columns(list(frame.columns)).get("effective_date")
        if index is None:
            return ""
        for value in frame.iloc[:, index]:
            text = str(value).strip()
            if text:
                return parse_effective_date(text)
        return ""

    def fetch(self, work_root: Path, store=None, limit: int | None = None) -> list[InputItem]:
        paths = self._paths()
        cap = limit if limit is not None else self.limit
        if cap:
            paths = paths[:cap]
        items = []
        for path in paths:
            raw = path.read_bytes()
            source_id = "file:" + hashlib.sha1(path.name.encode() + raw).hexdigest()
            if store is not None and store.source_uploaded(source_id):
                continue
            work_dir = Path(work_root) / safe_name(source_id.replace(":", "_"))
            work_dir.mkdir(parents=True, exist_ok=True)
            saved = work_dir / path.name
            saved.write_bytes(raw)
            if path.suffix.lower() == ".zip":
                files = extract_zip(saved, work_dir / path.stem)
            else:
                files = [saved]
            items.append(InputItem(source_id, work_dir, files, self._facts(path), raw, "directory",
                                   {"filename": path.name, "trunk_override": self.trunk_override}))
        return items
