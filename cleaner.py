"""Read messy carrier rate files (CSV / xlsx / zip) into a clean DataFrame of strings.

Everything is kept as text so rates such as ``0.0012`` are never turned into floats.
See AGENTS.md section 4 for the quirks this handles: BOM, ``#`` comment lines (including a
header written as a ``#`` line), fully blank rows and short rows.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = (".csv", ".xlsx", ".xls")
EXCEL_SUFFIXES = (".xlsx", ".xls")

# Tokens used to recognise a header row. Needed because Fractel writes its header as a '#' line.
HEADER_TOKENS = (
    "prefix", "dialcode", "dial code", "code", "npanxx",
    "inter", "interstate", "intra", "intrastate", "indeterminate",
    "destination", "name", "effective",
)


def _cell(value: object) -> str:
    """Trim a single cell and drop any BOM character."""
    return str(value).replace("\ufeff", "").strip()


def _rows_from_csv(raw: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:  # pragma: no cover - latin-1 never fails
            continue
    else:  # pragma: no cover
        text = raw.decode("utf-8", errors="replace")
    return [[_cell(c) for c in row] for row in csv.reader(io.StringIO(text))]


def _rows_from_excel(raw: bytes) -> list[list[str]]:
    frame = pd.read_excel(io.BytesIO(raw), dtype=str, header=None, keep_default_na=False)
    return [[_cell(c) for c in row] for row in frame.itertuples(index=False, name=None)]


def _rows_from_bytes(raw: bytes, name: str) -> list[list[str]]:
    if name.lower().endswith(EXCEL_SUFFIXES):
        return _rows_from_excel(raw)
    return _rows_from_csv(raw)


def _rows_from_zip(path: Path) -> list[list[str]]:
    """Return the rows of the (first) CSV/XLSX member inside a zip."""
    with zipfile.ZipFile(path) as zf:
        members = [
            m for m in zf.namelist()
            if not m.endswith("/") and Path(m).suffix.lower() in ARCHIVE_SUFFIXES
        ]
        if not members:
            raise ValueError(f"{path.name}: no CSV/XLSX file found inside the zip")
        if len(members) > 1:
            log.warning("%s: %d files in zip, using %s", path.name, len(members), members[0])
        return _rows_from_bytes(zf.read(members[0]), members[0])


def _looks_like_header(fields: list[str]) -> bool:
    joined = " ".join(f.lower() for f in fields)
    return sum(token in joined for token in HEADER_TOKENS) >= 2


def _split_header_and_rows(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    header: list[str] | None = None
    data: list[list[str]] = []
    for row in rows:
        if not row or all(c == "" for c in row):  # drop fully blank rows
            continue
        if row[0].startswith("#"):
            stripped = list(row)
            stripped[0] = stripped[0].lstrip("#").strip()
            if header is None and _looks_like_header(stripped):
                header = stripped  # a header written as a '#' line (Fractel)
            continue  # otherwise it is just a comment line
        if header is None:
            header = row
            continue
        data.append(row)
    if header is None:
        return [], []
    width = len(header)
    data = [r[:width] + [""] * (width - len(r)) for r in data]
    return header, data


def read_clean(path: str | Path) -> pd.DataFrame:
    """Read a carrier rate file into a DataFrame with trimmed string cells and no blank rows."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        rows = _rows_from_zip(path)
    else:
        rows = _rows_from_bytes(path.read_bytes(), path.name)
    header, data = _split_header_and_rows(rows)
    return pd.DataFrame(data, columns=header, dtype=str)
