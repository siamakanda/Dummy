"""Validation checks for a converted template deck (AGENTS.md section 6).

Errors block the upload; warnings require human approval. Checks are vectorised so they stay
fast on full decks (~240k rows).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping

import numpy as np
import pandas as pd

import config
from mapper import TEMPLATE_COLUMNS

RATE_COLUMNS = ("inter_rate", "intra_rate", "indeterminate_rate")
REQUIRED_COLUMNS = ("prefix", "inter_rate", "intra_rate")
INT_COLUMNS = ("init_duration", "increment")

DEFAULT_RATE_CHANGE_PCT = 20.0
DEFAULT_RATE_CHANGE_SHARE = 0.01

@dataclass
class Issue:
    level: str  # "error" | "warn"
    code: str
    message: str
    rows: list[int] = field(default_factory=list)

    def __str__(self) -> str:
        where = ""
        if self.rows:
            shown = ", ".join(str(r) for r in self.rows[:10])
            where = f" (rows {shown}{', ...' if len(self.rows) > 10 else ''})"
        return f"[{self.level}] {self.code}: {self.message}{where}"

def _rows(mask) -> list[int]:
    values = mask.to_numpy() if hasattr(mask, "to_numpy") else np.asarray(mask)
    return np.flatnonzero(values).tolist()

def _text(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip()

def _rate(text: object) -> Decimal | None:
    try:
        return Decimal(str(text).strip())
    except (InvalidOperation, ValueError):
        return None

def _is_rate(text: object) -> bool:
    value = _rate(text)
    return value is not None and value >= 0

def _is_positive_int(text: object) -> bool:
    try:
        return int(str(text).strip()) > 0
    except (TypeError, ValueError):
        return False

def _parses_date(text: object) -> bool:
    try:
        _dt.datetime.strptime(str(text).strip(), config.EFFECTIVE_DATE_FORMAT)
        return True
    except ValueError:
        return False

def has_errors(issues: list[Issue]) -> bool:
    return any(issue.level == "error" for issue in issues)

def validate(
    df: pd.DataFrame,
    previous: pd.DataFrame | None = None,
    overrides: Mapping | None = None,
) -> list[Issue]:
    options = overrides or {}
    issues: list[Issue] = []

    if list(df.columns) != TEMPLATE_COLUMNS:
        issues.append(Issue("error", "header_mismatch",
                            f"output header must be exactly {TEMPLATE_COLUMNS}"))

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        issues.append(Issue("error", "missing_columns", f"missing required columns: {missing}"))
        return issues

    prefix = _text(df["prefix"])
    is_digits = prefix.str.isdigit()
    is_npanxx = prefix.str.fullmatch(r"1\d{6}")
    bad_digits = _rows(~is_digits)
    unusual_length = _rows(is_digits & ~is_npanxx)
    if bad_digits:
        issues.append(Issue("error", "bad_prefix", "prefix must be digits only", bad_digits))
    if unusual_length:
        issues.append(Issue("warn", "prefix_length",
                            "prefix is not 7 digits starting with '1' (NPANXX) - confirm before upload",
                            unusual_length))

    duplicates = prefix.duplicated(keep=False)
    if bool(duplicates.any()):
        issues.append(Issue("error", "duplicate_prefix",
                            f"duplicate prefixes: {sorted(set(prefix[duplicates]))[:10]}",
                            _rows(duplicates)))

    for column in RATE_COLUMNS:
        if column not in df.columns:
            continue
        values = _text(df[column])
        empty = values.eq("")
        bad_values = {value for value in pd.unique(values) if value != "" and not _is_rate(value)}
        if bool(empty.any()):
            issues.append(Issue("error", "empty_rate", f"{column} is empty", _rows(empty)))
        if bad_values:
            issues.append(Issue("error", "bad_rate", f"{column} must be a number >= 0",
                                _rows(values.isin(bad_values))))

    for column in INT_COLUMNS:
        if column not in df.columns:
            continue
        values = _text(df[column])
        bad_values = {value for value in pd.unique(values) if not _is_positive_int(value)}
        if bad_values:
            issues.append(Issue("error", "bad_int_field",
                                f"{column} must be a positive integer",
                                _rows(values.isin(bad_values))))

    if "effective_date" in df.columns:
        values = _text(df["effective_date"])
        bad_values = {value for value in pd.unique(values) if value and not _parses_date(value)}
        if bad_values:
            issues.append(Issue("error", "bad_effective_date",
                                f"effective_date must match {config.EFFECTIVE_DATE_FORMAT}",
                                _rows(values.isin(bad_values))))

    if previous is not None:
        issues.extend(_compare_to_previous(df, previous, options))

    return issues

def _compare_to_previous(df: pd.DataFrame, previous: pd.DataFrame, options: Mapping) -> list[Issue]:
    issues: list[Issue] = []
    previous_count, count = len(previous), len(df)
    if previous_count and count < previous_count * 0.95:
        issues.append(Issue("warn", "row_count_drop",
                            f"row count dropped {previous_count} -> {count} (>5%)"))

    pct = float(options.get("rate_change_pct", DEFAULT_RATE_CHANGE_PCT))
    share = float(options.get("rate_change_share", DEFAULT_RATE_CHANGE_SHARE))
    if previous_count and count and "inter_rate" in previous.columns:
        prior = dict(zip(_text(previous["prefix"]), _text(previous["inter_rate"])))
        old = pd.to_numeric(_text(df["prefix"]).map(prior), errors="coerce")
        new = pd.to_numeric(_text(df["inter_rate"]), errors="coerce")
        changed_mask = (old.notna() & new.notna() & old.ne(0)
                        & ((new - old).abs() / old.abs() * 100 > pct))
        changed = _rows(changed_mask)
        if changed and len(changed) / count > share:
            issues.append(Issue("warn", "rate_change",
                                f"{len(changed)}/{count} prefixes changed by >{pct:g}% vs previous deck",
                                changed))
    return issues
