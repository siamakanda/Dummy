"""Bounded mapping agent: propose column aliases, convert, validate, and stop.

It is deliberately *not* free-roaming and never touches the portal. The only "tools" it can use are
local and read-only: ``missing``/``resolve`` (mapper), ``convert`` (to_template) and ``validate``. The
LLM can only propose aliases for columns that **actually exist** (validated by
``email_llm.propose_mapping``), so it can never invent a column. When it cannot finish, it returns
``needs_human`` - never a guess.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from mapper import TEMPLATE_COLUMNS, missing_required_columns, to_template
from validator import validate

log = logging.getLogger("dummy.mapping_agent")

MAX_STEPS = 3


@dataclass
class AgentResult:
    status: str                      # "mapped" | "needs_human"
    aliases: dict[str, list[str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    steps: int = 0
    notes: str = ""


def _default_propose(headers, rows, use_llm=None):
    try:
        from email_llm import propose_mapping
        return propose_mapping(headers, rows, use_llm=use_llm) or {}
    except Exception as exc:                      # no key / network / bad JSON -> no suggestion
        log.warning("mapping agent: LLM proposal unavailable (%s)", exc)
        return {}


def _convert_and_validate(raw: pd.DataFrame, aliases: dict[str, list[str]]):
    try:
        frame = to_template(raw, {"header_aliases": aliases})
    except Exception as exc:
        return None, [f"convert failed: {exc}"]
    issues = validate(frame)
    return frame, [str(issue) for issue in issues if issue.level == "error"]


def analyze(raw: pd.DataFrame, *, base_aliases: dict | None = None, max_steps: int = MAX_STEPS,
            use_llm: bool | None = None, propose=None) -> AgentResult:
    """Run the bounded loop and return the mapping it verified (or why it stopped)."""
    propose = propose or _default_propose
    aliases = dict(base_aliases or {})
    steps = 0

    while True:
        missing = missing_required_columns(list(raw.columns), aliases)
        if not missing:
            break
        if steps >= max_steps:
            return AgentResult("needs_human", aliases, missing=missing, steps=steps,
                               notes=f"could not map: {', '.join(missing)}")
        steps += 1
        proposal = propose(list(raw.columns), raw.head(5).to_dict("records"), use_llm=use_llm) or {}
        proposal = {key: value for key, value in proposal.items() if key in TEMPLATE_COLUMNS}
        merged = {**aliases, **proposal}
        if merged == aliases:                     # the LLM had nothing new to offer
            return AgentResult("needs_human", aliases, missing=missing, steps=steps,
                               notes=f"could not map: {', '.join(missing)}")
        aliases = merged

    frame, errors = _convert_and_validate(raw, aliases)
    if errors:
        return AgentResult("needs_human", aliases, errors=errors[:5], steps=steps,
                           notes="columns mapped, but the converted deck failed validation")
    return AgentResult("mapped", aliases, steps=steps, notes=f"{len(frame)} rows convert cleanly")
