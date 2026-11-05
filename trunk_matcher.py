"""Decide which trunk a rate deck belongs to (AGENTS.md sections 5 and 7).

Strategy: sender domain -> carrier, then tech prefix (exact) or product/trunk name (rapidfuzz).
Returns the best trunk with a confidence score and the candidate list. A sender->carrier match
alone is never a positive signal, so a single-trunk carrier still goes to a human.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel
from rapidfuzz import fuzz

import config

CARRIER_BOOST = 0.1      # applied to candidates whose carrier matches the sender
AMBIGUITY_GAP = 0.05     # top two this close (and not exact) => ask a human

class TrunkHints(BaseModel):
    """Hints extracted from a rate-deck email (M6 will produce this)."""
    sender: str = ""
    sender_domain: str = ""
    tech_prefix: str = ""
    trunk_name: str = ""
    product: str = ""
    deck_name: str = ""
    carrier_name: str = ""

@dataclass
class Candidate:
    trunk_id: int
    trunk_name: str
    carrier_id: int
    carrier_name: str
    score: float
    reason: str
    carrier_match: bool = False

@dataclass
class MatchResult:
    trunk_id: int | None
    trunk_name: str
    carrier_id: int | None
    carrier_name: str
    confidence: float
    reason: str
    candidates: list[Candidate] = field(default_factory=list)
    ambiguous: bool = False
    threshold: float = config.MATCH_THRESHOLD

    @property
    def needs_human(self) -> bool:
        return self.trunk_id is None or self.ambiguous or self.confidence < self.threshold

def _norm_prefix(value: object) -> str:
    return str(value or "").strip().lstrip("+")

def _name_queries(hints: TrunkHints) -> list[str]:
    """All name/phrase candidates to try (trunk name, product, deck filename)."""
    queries, seen = [], set()
    for value in (hints.trunk_name, hints.product, hints.deck_name):
        text = (value or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            queries.append(text)
    return queries

def _fuzzy_score(query: str, trunk: dict) -> float:
    if not query:
        return 0.0
    by_name = fuzz.WRatio(query, str(trunk.get("trunk_name") or ""))
    by_deck = fuzz.WRatio(query, str(trunk.get("cost_deck") or ""))
    return max(by_name, by_deck) / 100.0

def _alias_match(query: str, trunk: dict) -> bool:
    """A human-declared name alias (exact, case-insensitive) beats fuzzy guessing."""
    if not query:
        return False
    aliases = trunk.get("name_aliases") or []
    if isinstance(aliases, str):
        aliases = json.loads(aliases or "[]")
    target = query.strip().casefold()
    return any(str(alias).strip().casefold() == target for alias in aliases)

def carrier_from_sender(domain: str, store) -> int | None:
    """Look up the carrier id for a sender domain via the store (None if unknown)."""
    if not domain:
        return None
    return store.carrier_for_domain(domain)

def match_trunks(
    hints: TrunkHints,
    trunks: list[dict],
    *,
    sender_carrier: int | None = None,
    threshold: float | None = None,
) -> MatchResult:
    threshold = config.MATCH_THRESHOLD if threshold is None else threshold
    prefix = _norm_prefix(hints.tech_prefix)
    queries = _name_queries(hints)

    candidates: list[Candidate] = []
    for trunk in trunks:
        trunk_id, carrier_id = int(trunk["trunk_id"]), int(trunk["carrier_id"])
        carrier_match = sender_carrier is not None and carrier_id == sender_carrier
        score, reason = 0.0, ""
        trunk_prefix = _norm_prefix(trunk.get("prefix"))
        if prefix and trunk_prefix and prefix == trunk_prefix:
            score, reason = 1.0, f"tech prefix {prefix} matches"
        elif any(_alias_match(name, trunk) for name in queries):
            score, reason = 1.0, "name alias matches"
        elif queries:
            score = max(_fuzzy_score(name, trunk) for name in queries)
            if score > 0:
                reason = f"name match {score:.2f}"
        if score > 0 and carrier_match:
            score = min(1.0, score + CARRIER_BOOST)
            reason += " + sender carrier"
        candidates.append(Candidate(
            trunk_id=trunk_id, trunk_name=str(trunk.get("trunk_name") or ""),
            carrier_id=carrier_id, carrier_name=str(trunk.get("carrier_name") or ""),
            score=round(score, 3), reason=reason, carrier_match=carrier_match))

    # best score first, then the sender's carrier, then a stable name order
    candidates.sort(key=lambda c: (-c.score, not c.carrier_match, c.trunk_name))

    if not candidates:
        return MatchResult(None, "", sender_carrier, "", 0.0, "no trunks available",
                           candidates, False, threshold)

    top = candidates[0]
    if top.score <= 0:
        return MatchResult(None, "", sender_carrier, "", 0.0, "no trunk signal in the email",
                           candidates, False, threshold)

    # Ambiguous when a runner-up is within AMBIGUITY_GAP, unless the sender's carrier uniquely
    # picks the top (i.e. top matches the carrier and no runner-up does).
    runners = [c for c in candidates[1:] if top.score - c.score < AMBIGUITY_GAP]
    disambiguated = top.carrier_match and not any(r.carrier_match for r in runners)
    ambiguous = bool(runners) and not disambiguated

    return MatchResult(top.trunk_id, top.trunk_name, top.carrier_id, top.carrier_name,
                       top.score, top.reason, candidates, ambiguous, threshold)
