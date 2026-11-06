"""SQLite persistence for Dummy (AGENTS.md section 5).

Keeps the trunk inventory, per-trunk config, first-time approvals, and the audit trail
(messages / runs / decisions). Only stdlib ``sqlite3``.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS carriers (
    carrier_id   INTEGER PRIMARY KEY,
    carrier_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trunks (
    trunk_id     INTEGER PRIMARY KEY,
    carrier_id   INTEGER NOT NULL,
    carrier_name TEXT,
    trunk_name   TEXT NOT NULL,
    prefix       TEXT DEFAULT '',
    status       TEXT DEFAULT '',
    cost_deck    TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS carrier_senders (
    domain     TEXT PRIMARY KEY,
    carrier_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trunk_config (
    trunk_id              INTEGER PRIMARY KEY,
    carrier_id            INTEGER,
    tech_prefix           TEXT DEFAULT '',
    default_init_duration INTEGER,
    default_increment     INTEGER,
    header_aliases        TEXT DEFAULT '{}',
    name_aliases          TEXT DEFAULT '[]',
    updated_at            TEXT
);
CREATE TABLE IF NOT EXISTS approved_trunks (
    trunk_id    INTEGER PRIMARY KEY,
    carrier_id  INTEGER,
    approved_at TEXT,
    approved_by TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,
    received_at  TEXT,
    sender       TEXT,
    subject      TEXT,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    trunk_id   INTEGER,
    status     TEXT,
    notes      TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,
    created_at  TEXT,
    kind        TEXT,
    question    TEXT,
    answer      TEXT,
    actor       TEXT
);
CREATE TABLE IF NOT EXISTS deck_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trunk_id       INTEGER,
    deck_id        INTEGER,
    effective_date TEXT,
    rows           INTEGER,
    changed        INTEGER,
    converted_path TEXT,
    recorded_at    TEXT
);
CREATE TABLE IF NOT EXISTS pending (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         TEXT,
    run_id            INTEGER,
    kind              TEXT,                 -- trunk | approval | warnings
    status            TEXT DEFAULT 'open',  -- open | resolved | committed | failed | cancelled
    trunk_id          INTEGER,
    resolved_trunk_id INTEGER,
    approved          INTEGER DEFAULT 0,
    work_dir          TEXT,
    converted_path    TEXT,
    effective_date    TEXT,
    rows              INTEGER,
    changes           INTEGER,
    reasons           TEXT DEFAULT '[]',
    created_at        TEXT,
    resolved_at       TEXT,
    resolved_by       TEXT
);
CREATE TABLE IF NOT EXISTS deck_jobs (
    job_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT UNIQUE,
    file_path      TEXT,
    file_name      TEXT,
    candidates     TEXT DEFAULT '[]',
    trunk_id       INTEGER,
    status         TEXT DEFAULT 'unmapped',   -- unmapped | mapped | dry_run | uploaded | assigned | ...
    effective_date TEXT,
    converted_path TEXT,
    deck_id        INTEGER,
    deck_name      TEXT,
    suggested_aliases TEXT,
    deck_created_at TEXT,
    date_hint      TEXT,
    date_source    TEXT,
    rows           INTEGER DEFAULT 0,
    changes        INTEGER DEFAULT 0,
    notes          TEXT,
    created_at     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class Store:
    """Thin SQLite wrapper. Creates its schema on open."""

    def __init__(self, path: str | Path = config.DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")     # concurrent job threads + dashboard reads
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        for statement in (                     # migrate older DBs
            "ALTER TABLE deck_history ADD COLUMN deck_id INTEGER",
            "ALTER TABLE trunk_config ADD COLUMN name_aliases TEXT DEFAULT '[]'",
            "ALTER TABLE deck_jobs ADD COLUMN deck_id INTEGER",
            "ALTER TABLE deck_jobs ADD COLUMN deck_name TEXT",
            "ALTER TABLE deck_jobs ADD COLUMN suggested_aliases TEXT",
            "ALTER TABLE deck_jobs ADD COLUMN deck_created_at TEXT",
            "ALTER TABLE deck_jobs ADD COLUMN date_hint TEXT",
            "ALTER TABLE deck_jobs ADD COLUMN date_source TEXT",
        ):
            try:
                self.conn.execute(statement)
            except sqlite3.OperationalError:
                pass
        # one-time: "uploaded" used to mean uploaded AND assigned; the split makes it uploaded-only.
        if self.conn.execute("SELECT value FROM meta WHERE key = 'deck_status_split'").fetchone() is None:
            self.conn.execute("UPDATE deck_jobs SET status = 'assigned' WHERE status = 'uploaded'")
            self.conn.execute("INSERT INTO meta(key, value) VALUES('deck_status_split', '1')")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def open(cls, path: str | Path | None = None) -> "Store":
        """Open a store and, if it has no trunks yet, bootstrap from data/trunks.csv."""
        store = cls(path or config.DB_PATH)
        if not store.all_trunks() and (config.DATA_DIR / "trunks.csv").exists():
            store.load_trunks(config.DATA_DIR / "trunks.csv")
        return store

    # ---- trunks / carriers ----------------------------------------------
    def load_trunks(self, csv_path: str | Path) -> int:
        """Load data/trunks.csv (carrier + trunk list) into the DB. Returns rows read."""
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        loaded = 0
        with self.conn:
            for row in rows:
                trunk_id, carrier_id = str(row.get("trunk_id", "")), str(row.get("carrier_id", ""))
                if not trunk_id.isdigit() or not carrier_id.isdigit():
                    continue
                carrier_name = row.get("carrier_name", "")
                self.conn.execute(
                    "INSERT INTO carriers(carrier_id, carrier_name) VALUES(?, ?) "
                    "ON CONFLICT(carrier_id) DO UPDATE SET carrier_name=excluded.carrier_name",
                    (int(carrier_id), carrier_name))
                self.conn.execute(
                    "INSERT INTO trunks(trunk_id, carrier_id, carrier_name, trunk_name, prefix, status, cost_deck) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(trunk_id) DO UPDATE SET carrier_id=excluded.carrier_id, "
                    "carrier_name=excluded.carrier_name, trunk_name=excluded.trunk_name, "
                    "prefix=excluded.prefix, status=excluded.status, cost_deck=excluded.cost_deck",
                    (int(trunk_id), int(carrier_id), carrier_name, row.get("trunk_name", ""),
                     row.get("prefix", ""), row.get("status", ""), row.get("cost_deck", "")))
                loaded += 1
        return loaded

    def all_trunks(self, carrier_id: int | None = None) -> list[dict]:
        query = ("SELECT t.*, COALESCE(c.name_aliases, '[]') AS name_aliases "
                 "FROM trunks t LEFT JOIN trunk_config c ON c.trunk_id = t.trunk_id")
        args: tuple = ()
        if carrier_id is not None:
            query += " WHERE t.carrier_id = ?"
            args = (carrier_id,)
        query += " ORDER BY t.carrier_id, t.trunk_name"
        rows = []
        for row in self.conn.execute(query, args):
            item = dict(row)
            item["name_aliases"] = json.loads(item.get("name_aliases") or "[]")
            rows.append(item)
        return rows

    def get_trunk(self, trunk_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM trunks WHERE trunk_id = ?", (trunk_id,)).fetchone()
        return dict(row) if row else None

    def list_carriers(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM carriers ORDER BY carrier_id")]

    # ---- sender domain -> carrier ---------------------------------------
    def set_sender_domain(self, domain: str, carrier_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO carrier_senders(domain, carrier_id) VALUES(?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET carrier_id=excluded.carrier_id",
                (domain.strip().lower(), carrier_id))

    def carrier_for_domain(self, domain: str) -> int | None:
        row = self.conn.execute(
            "SELECT carrier_id FROM carrier_senders WHERE domain = ?",
            (domain.strip().lower(),)).fetchone()
        return int(row["carrier_id"]) if row else None

    def list_sender_domains(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM carrier_senders ORDER BY domain")]

    # ---- per-trunk config ------------------------------------------------
    def set_config(self, trunk_id: int, **fields) -> None:
        current = self.get_config(trunk_id) or {}
        merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
        with self.conn:
            self.conn.execute(
                "INSERT INTO trunk_config(trunk_id, carrier_id, tech_prefix, default_init_duration, "
                "default_increment, header_aliases, name_aliases, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trunk_id) DO UPDATE SET carrier_id=excluded.carrier_id, "
                "tech_prefix=excluded.tech_prefix, default_init_duration=excluded.default_init_duration, "
                "default_increment=excluded.default_increment, header_aliases=excluded.header_aliases, "
                "name_aliases=excluded.name_aliases, updated_at=excluded.updated_at",
                (trunk_id, merged.get("carrier_id"), merged.get("tech_prefix", ""),
                 merged.get("default_init_duration"), merged.get("default_increment"),
                 json.dumps(merged.get("header_aliases", {})),
                 json.dumps(merged.get("name_aliases", [])), _now()))

    def get_config(self, trunk_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM trunk_config WHERE trunk_id = ?", (trunk_id,)).fetchone()
        if not row:
            return None
        config_row = dict(row)
        config_row["header_aliases"] = json.loads(config_row.get("header_aliases") or "{}")
        config_row["name_aliases"] = json.loads(config_row.get("name_aliases") or "[]")
        return config_row

    # ---- approvals -------------------------------------------------------
    def approve(self, trunk_id: int, by: str = "human", carrier_id: int | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO approved_trunks(trunk_id, carrier_id, approved_at, approved_by) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(trunk_id) DO UPDATE SET approved_at=excluded.approved_at, "
                "approved_by=excluded.approved_by",
                (trunk_id, carrier_id, _now(), by))

    def is_approved(self, trunk_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM approved_trunks WHERE trunk_id = ?", (trunk_id,)).fetchone()
        return row is not None

    # ---- messages / runs / decisions (audit trail) ----------------------
    def add_message(self, message_id: str, received_at: str | None = None,
                    sender: str = "", subject: str = "") -> bool:
        """Insert a message; returns True if it was new (idempotency by Message-ID)."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO messages(message_id, received_at, sender, subject, processed_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (message_id, received_at, sender, subject, _now()))
        return cursor.rowcount > 0

    def has_message(self, message_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)).fetchone() is not None

    def start_run(self, message_id: str) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs(message_id, started_at, status) VALUES(?, ?, ?)",
                (message_id, _now(), "started"))
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, trunk_id: int | None = None,
                   status: str = "done", notes: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, trunk_id = ?, status = ?, notes = ? WHERE run_id = ?",
                (_now(), trunk_id, status, notes, run_id))

    def add_decision(self, run_id: int, kind: str, question: str,
                     answer: str, actor: str = "human") -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO decisions(run_id, created_at, kind, question, answer, actor) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (run_id, _now(), kind, question, answer, actor))
        return int(cursor.lastrowid)

    # ---- deck history ----------------------------------------------------
    def record_deck(self, trunk_id: int, effective_date: str | None, rows: int,
                    changed: int, converted_path, deck_id: int | None = None) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO deck_history(trunk_id, deck_id, effective_date, rows, changed, "
                "converted_path, recorded_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (trunk_id, deck_id, effective_date, rows, changed, str(converted_path), _now()))
        return int(cursor.lastrowid)

    def last_deck(self, trunk_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM deck_history WHERE trunk_id = ? ORDER BY id DESC LIMIT 1",
            (trunk_id,)).fetchone()
        return dict(row) if row else None

    def trunk_job_counts(self) -> dict[int, dict[str, int]]:
        """{trunk_id: {status: count}} from deck_jobs (for the Trunks view)."""
        counts: dict[int, dict[str, int]] = {}
        for row in self.conn.execute(
                "SELECT trunk_id, status, COUNT(*) AS n FROM deck_jobs "
                "WHERE trunk_id IS NOT NULL GROUP BY trunk_id, status"):
            counts.setdefault(int(row["trunk_id"]), {})[row["status"]] = int(row["n"])
        return counts

    def previous_deck_path(self, trunk_id: int, exclude_job_id: int | None = None) -> str | None:
        """Most recent converted deck for a trunk (from deck_jobs) - used for change warnings."""
        query = ("SELECT converted_path FROM deck_jobs WHERE trunk_id = ? "
                 "AND converted_path IS NOT NULL AND converted_path != '' "
                 "AND status IN ('dry_run', 'uploaded', 'assigned')")
        args: list = [trunk_id]
        if exclude_job_id is not None:
            query += " AND job_id != ?"
            args.append(exclude_job_id)
        query += " ORDER BY updated_at DESC, job_id DESC LIMIT 1"
        row = self.conn.execute(query, tuple(args)).fetchone()
        return row["converted_path"] if row else None

    def source_uploaded(self, source_id: str) -> bool:
        """True if this source (Message-ID / file hash) was already successfully uploaded."""
        row = self.conn.execute(
            "SELECT 1 FROM runs WHERE message_id = ? AND status = 'uploaded' LIMIT 1",
            (source_id,)).fetchone()
        return row is not None

    # ---- pending queue (human-in-the-loop) -------------------------------
    def add_pending(self, source_id: str, *, kind: str, run_id: int | None = None,
                    trunk_id: int | None = None, work_dir=None, converted_path=None,
                    effective_date: str = "", rows: int = 0, changes: int = 0,
                    reasons=None) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO pending(source_id, run_id, kind, status, trunk_id, work_dir, "
                "converted_path, effective_date, rows, changes, reasons, created_at) "
                "VALUES(?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, run_id, kind, trunk_id, str(work_dir), str(converted_path),
                 effective_date, rows, changes, json.dumps(reasons or []), _now()))
        return int(cursor.lastrowid)

    def list_pending(self, status: str | None = "open") -> list[dict]:
        query = "SELECT * FROM pending"
        args: tuple = ()
        if status:
            query += " WHERE status = ?"
            args = (status,)
        query += " ORDER BY id DESC"
        return [dict(row) for row in self.conn.execute(query, args)]

    def get_pending(self, pending_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM pending WHERE id = ?", (pending_id,)).fetchone()
        return dict(row) if row else None

    def open_pending_for(self, source_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pending WHERE source_id = ? AND status IN ('open', 'resolved') "
            "ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()
        return dict(row) if row else None

    def resolve_pending(self, pending_id: int, *, trunk_id: int | None = None,
                        approve: bool = False, by: str = "human") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE pending SET status = 'resolved', "
                "resolved_trunk_id = COALESCE(?, resolved_trunk_id), "
                "approved = CASE WHEN ? THEN 1 ELSE approved END, resolved_at = ?, resolved_by = ? "
                "WHERE id = ?",
                (trunk_id, 1 if approve else 0, _now(), by, pending_id))

    def set_pending_status(self, pending_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE pending SET status = ? WHERE id = ?", (status, pending_id))

    def reopen_pending(self, pending_id: int, *, kind: str = "approval",
                       trunk_id: int | None = None, reasons=None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE pending SET status = 'open', kind = ?, trunk_id = COALESCE(?, trunk_id), "
                "reasons = ?, resolved_at = NULL, resolved_by = NULL WHERE id = ?",
                (kind, trunk_id, json.dumps(reasons or []), pending_id))

    # ---- folder-first engine jobs ---------------------------------------
    def add_job(self, source_id: str, *, file_path="", file_name="", candidates=None,
                effective_date="", date_hint="", date_source="") -> int | None:
        """Create a deck job (idempotent by source_id); returns None if it already existed."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO deck_jobs(source_id, file_path, file_name, candidates, effective_date, "
                "date_hint, date_source, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'unmapped', ?, ?) "
                "ON CONFLICT(source_id) DO NOTHING",
                (source_id, str(file_path), file_name, json.dumps(candidates or []),
                 effective_date, date_hint, date_source, _now(), _now()))
        return int(cursor.lastrowid) if cursor.rowcount else None

    @staticmethod
    def _job(row) -> dict:
        item = dict(row)
        item["candidates"] = json.loads(item.get("candidates") or "[]")
        item["suggested_aliases"] = json.loads(item.get("suggested_aliases") or "{}")
        return item

    def list_jobs(self, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM deck_jobs"
        args: tuple = ()
        if status:
            query += " WHERE status = ?"
            args = (status,)
        query += " ORDER BY job_id DESC"
        return [self._job(row) for row in self.conn.execute(query, args)]

    def get_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM deck_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def set_job_mapping(self, job_id: int, trunk_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE deck_jobs SET trunk_id = ?, status = 'mapped', updated_at = ? "
                              "WHERE job_id = ?", (trunk_id, _now(), job_id))

    def update_job(self, job_id: int, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self.conn:
            self.conn.execute(f"UPDATE deck_jobs SET {columns}, updated_at = ? WHERE job_id = ?",
                              (*fields.values(), _now(), job_id))

    # ---- read helpers for the web admin ---------------------------------
    def list_runs(self, limit: int = 30) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,))]

    def list_decks(self, limit: int = 30) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM deck_history ORDER BY id DESC LIMIT ?", (limit,))]
