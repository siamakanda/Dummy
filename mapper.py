"""Map a cleaned carrier rate file onto our fixed output template (AGENTS.md sections 3 and 4).

Rates are copied through as exact strings; ``Decimal`` is used only to compare inter vs intra
when the carrier gives no indeterminate rate. All operations are vectorised so full decks
(hundreds of thousands of rows) convert in a couple of seconds.
"""
from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

import config

TEMPLATE_COLUMNS = [
    "prefix", "destination", "inter_rate", "intra_rate", "indeterminate_rate",
    "connect_fee", "init_duration", "increment", "grace_time", "effective_date",
]

# Canonical column -> header names (normalised: lowercase, single spaces) seen in carrier files.
BUILTIN_ALIASES: dict[str, list[str]] = {
    "prefix": ["prefix", "dialcode", "dial code", "code", "1npanxx", "npanxx", "dial_code"],
    "destination": ["destination", "name", "dest", "description"],
    "inter_rate": ["inter rate", "interstate rate", "interstate", "rate (inter)", "inter"],
    "intra_rate": ["intra rate", "intrastate rate", "intrastate", "rate (intra)", "intra"],
    "indeterminate_rate": ["indeterminate rate", "indeterminate"],
    "connect_fee": ["connect fee", "connect"],
    "init_duration": ["init duration", "min dur", "min duration", "initial duration"],
    "increment": ["increment", "inc dur", "inc duration"],
    "grace_time": ["grace time", "grace"],
    "effective_date": ["effective date", "effectivedateutc", "effective"],
}

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",   # Fractel: 2026-11-01 00:00:00
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y %H:%M",      # Fractel (older): 11/1/2026 0:00
    "%m/%d/%Y",            # BLC/CONVO: 11/2/2026
    "%m/%d/%y",            # BLC: 11/2/26
    "%Y-%m-%d",            # ISO: 2026-11-01
)

def _norm(name: object) -> str:
    text = str(name).replace("\ufeff", "").replace("_", " ")
    return " ".join(text.strip().lower().split())

def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(str(text).strip())
    except (InvalidOperation, ValueError):
        return None

def parse_effective_date(text: str) -> str:
    """Format a carrier date using EFFECTIVE_DATE_FORMAT, or pass it through untouched."""
    text = (text or "").strip()
    if not text:
        return ""
    for fmt in DATE_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt).strftime(config.EFFECTIVE_DATE_FORMAT)
        except ValueError:
            continue
    return text  # unparseable; the validator will flag it

def parse_date_from_filename(name: str) -> str:
    """Best-effort ISO date embedded in a filename (e.g. ``..._E20261113_...``); ``""`` if none.

    This is a **hint only** - filenames can lie (ANI ``10_28`` was effective Oct 29), so callers show
    it to a human instead of using it automatically.
    """
    text = str(name or "")
    patterns = (
        (r"(?i)(?<![A-Za-z])E(20\d{2})(\d{2})(\d{2})(?!\d)", "ymd"),
        (r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", "ymd"),
        (r"(20\d{2})[-_](\d{2})[-_](\d{2})", "ymd"),
        (r"(?<!\d)(\d{2})[-_](\d{2})[-_](20\d{2})(?!\d)", "mdy"),
        (r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)", "mdy"),
    )
    for pattern, order in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        a, b, c = match.groups()
        year, month, day = (a, b, c) if order == "ymd" else (c, a, b)
        try:
            date = _dt.date(int(year), int(month), int(day))
        except (TypeError, ValueError):
            continue
        if 2000 <= date.year <= 2100:
            return date.strftime(config.EFFECTIVE_DATE_FORMAT)
    return ""

def resolve_columns(
    headers: Iterable[str], extra_aliases: Mapping[str, Iterable[str]] | None = None
) -> dict[str, int]:
    """Return {canonical_name: column_index} using built-in aliases plus any overrides."""
    aliases = {key: [_norm(n) for n in names] for key, names in BUILTIN_ALIASES.items()}
    for key, names in (extra_aliases or {}).items():
        aliases.setdefault(key, [])
        aliases[key] += [_norm(n) for n in names]

    normalised = {i: _norm(h) for i, h in enumerate(headers)}
    resolved: dict[str, int] = {}
    used: set[int] = set()
    for canonical, names in aliases.items():
        for index, header in normalised.items():
            if header in names and index not in used:
                resolved[canonical] = index
                used.add(index)
                break
    return resolved

LAYOUT_REQUIRED = ("prefix", "inter_rate", "intra_rate")


def missing_required_columns(headers, extra_aliases: Mapping[str, Iterable[str]] | None = None) -> list[str]:
    """Canonical columns the mapper cannot resolve from these headers (unknown layout)."""
    resolved = resolve_columns(headers, extra_aliases)
    return [name for name in LAYOUT_REQUIRED if name not in resolved]

def to_template(df: pd.DataFrame, trunk_config: Mapping | None = None) -> pd.DataFrame:
    """Convert a cleaned carrier frame into the fixed template (all cells strings)."""
    cfg = trunk_config or {}
    columns = resolve_columns(list(df.columns), cfg.get("header_aliases"))
    default_init = str(cfg.get("default_init_duration") or config.DEFAULT_INIT_DURATION)
    default_increment = str(cfg.get("default_increment") or config.DEFAULT_INCREMENT)
    size = len(df)

    def source(canonical: str) -> pd.Series:
        index = columns.get(canonical)
        if index is None:
            return pd.Series([""] * size, index=df.index, dtype="string")
        return df.iloc[:, index].astype("string").fillna("").str.strip()

    prefix = source("prefix")
    inter = source("inter_rate")
    intra = source("intra_rate")
    indeterminate = source("indeterminate_rate")
    connect = source("connect_fee")
    init = source("init_duration")
    increment = source("increment")
    grace = source("grace_time")
    effective = source("effective_date")

    # indeterminate = MAX(inter, intra) where the carrier gave none (compare with Decimal, keep text)
    blank = indeterminate.eq("")
    if bool(blank.any()):
        inter_values, intra_values = inter.to_numpy(), intra.to_numpy()
        indeterminate_values = indeterminate.to_numpy().copy()
        for i in np.flatnonzero(blank.to_numpy()):
            left, right = inter_values[i], intra_values[i]
            a, b = _decimal(left), _decimal(right)
            if a is not None and b is not None:
                indeterminate_values[i] = left if a >= b else right
            else:
                indeterminate_values[i] = left or right
        indeterminate = pd.Series(indeterminate_values, index=df.index, dtype="string")

    date_map = {value: parse_effective_date(value) for value in pd.unique(effective)}
    effective_out = effective.map(date_map)

    out = pd.DataFrame({
        "prefix": prefix,
        "destination": pd.Series(["NANPA"] * size, index=df.index, dtype="string"),
        "inter_rate": inter,
        "intra_rate": intra,
        "indeterminate_rate": indeterminate,
        "connect_fee": connect.mask(connect.eq(""), "0"),
        "init_duration": init.mask(init.eq(""), default_init),
        "increment": increment.mask(increment.eq(""), default_increment),
        "grace_time": grace,
        "effective_date": effective_out,
    })
    return out[TEMPLATE_COLUMNS]
