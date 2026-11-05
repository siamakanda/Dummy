"""Understand a rate-deck email: extract structured facts (AGENTS.md sections 5 and 7).

DeepSeek (any OpenAI-compatible endpoint) is the default LLM, but a rule-based extractor always
runs, so the pipeline works with no key. The LLM only reads email text - it never edits CSV rows.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import pathlib
import re
import sys

import requests
from pydantic import BaseModel, ConfigDict

import config
from mailbox import ParsedEmail, parse_email
from mapper import _norm
from trunk_matcher import TrunkHints

log = logging.getLogger(__name__)

DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y")
MONTH_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y")
DATE_TOKEN_RE = re.compile(r"\d{1,4}[/-]\d{1,2}[/-]\d{1,4}")
MONTH_TOKEN_RE = re.compile(r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}")
DATE_IN_NAME_RE = re.compile(r"(\d{4})[-_/]?(\d{2})[-_/]?(\d{2})")
PREFIX_RE = re.compile(r"(?:tech(?:nical)?\s*prefix|prefix)\s*[:\-]?\s*(\d{2,10})", re.IGNORECASE)
TRUNK_RE = re.compile(r"trunk(?:\s*name)?\s*[:\-]\s*([^\n\r]+)", re.IGNORECASE)
PRODUCT_RE = re.compile(r"product\s*[:\-]\s*([^\n\r]+)", re.IGNORECASE)
EFFECTIVE_RE = re.compile(r"effective(?:\s*date)?\s*[:\-]?\s*([^\n\r]+)", re.IGNORECASE)
INCREMENT_RE = re.compile(r"(?<![\d/-])(\d{1,3})\s*/\s*(\d{1,3})(?![\d/-])")
INCREMENT_HINTS = ("increment", "billing", "init duration", "min dur")

SYSTEM_PROMPT = (
    "You extract structured facts from carrier rate-deck emails. "
    "Reply with ONLY a JSON object with keys: carrier_name, trunk_name, tech_prefix, product, "
    "effective_date (ISO YYYY-MM-DD or empty string), init_duration (integer or null), "
    "increment (integer or null), deck_filename, notes. "
    "Use empty string / null when the email does not say. Never invent a trunk name or prefix."
)


class EmailFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    carrier_name: str = ""
    sender_domain: str = ""
    trunk_name: str = ""
    tech_prefix: str = ""
    product: str = ""
    effective_date: str = ""
    init_duration: int | None = None
    increment: int | None = None
    deck_filename: str = ""
    notes: str = ""

    def to_hints(self) -> TrunkHints:
        deck = pathlib.Path(self.deck_filename).stem if self.deck_filename else ""
        return TrunkHints(sender_domain=self.sender_domain, carrier_name=self.carrier_name,
                          trunk_name=self.trunk_name, tech_prefix=self.tech_prefix,
                          product=self.product, deck_name=deck.replace("_", " ").strip())


def _iso_date(text: str) -> str:
    for match in MONTH_TOKEN_RE.finditer(text or ""):
        token = match.group(0)
        for fmt in MONTH_FORMATS:
            try:
                return _dt.datetime.strptime(token, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    for match in DATE_TOKEN_RE.finditer(text or ""):
        token = match.group(0)
        for fmt in DATE_FORMATS:
            try:
                return _dt.datetime.strptime(token, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


def _effective_date(text: str) -> str:
    match = EFFECTIVE_RE.search(text or "")
    if match:
        found = _iso_date(match.group(1))
        if found:
            return found
    for line in (text or "").splitlines():
        if "effective" in line.lower():
            found = _iso_date(line)
            if found:
                return found
    return ""


def _increments(text: str) -> tuple[int | None, int | None]:
    chunks = []
    for line in (text or "").splitlines():
        if "->" in line:
            chunks.append(line.split("->", 1)[1])
        if any(hint in line.lower() for hint in INCREMENT_HINTS):
            chunks.append(line)
    for chunk in chunks:
        match = INCREMENT_RE.search(chunk)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None, None


def _name_date_key(name: str) -> tuple[int, int, int]:
    match = DATE_IN_NAME_RE.search(name or "")
    return tuple(int(g) for g in match.groups()) if match else (0, 0, 0)


def _deck_filename(parsed: ParsedEmail) -> str:
    names = [a.filename for a in parsed.attachments if a.filename]
    if names:
        return max(names, key=_name_date_key) if len(names) > 1 else names[-1]
    for url in parsed.download_links():
        base = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if base:
            return base
    return ""


def rule_based_facts(parsed: ParsedEmail) -> EmailFacts:
    text = f"{parsed.subject}\n{parsed.body_text}"
    prefix_match = PREFIX_RE.search(text)
    trunk_match = TRUNK_RE.search(parsed.body_text)
    product_match = PRODUCT_RE.search(text)
    init, increment = _increments(parsed.body_text)
    return EmailFacts(
        sender_domain=parsed.sender.split("@")[-1].lower() if "@" in parsed.sender else "",
        trunk_name=trunk_match.group(1).strip() if trunk_match else "",
        tech_prefix=prefix_match.group(1) if prefix_match else "",
        product=product_match.group(1).strip() if product_match else "",
        effective_date=_effective_date(parsed.body_text),
        init_duration=init,
        increment=increment,
        deck_filename=_deck_filename(parsed),
    )


class LLMClient:
    """Minimal OpenAI-compatible chat client (works with DeepSeek, OpenAI, …)."""

    def __init__(self, base_url=None, api_key=None, model=None, timeout=60):
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.api_key = api_key or config.LLM_API_KEY
        self.model = model or config.LLM_MODEL
        self.timeout = timeout

    def complete_json(self, system: str, user: str) -> str:
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def _build_user_prompt(parsed: ParsedEmail) -> str:
    attachments = ", ".join(a.filename for a in parsed.attachments) or "(none)"
    links = ", ".join(parsed.links) or "(none)"
    return (
        f"From: {parsed.sender}\nSubject: {parsed.subject}\n"
        f"Attachments: {attachments}\nLinks: {links}\n\n"
        f"Body:\n{parsed.body_text[:6000]}"
    )


def _overlay(base: EmailFacts, extra: EmailFacts) -> EmailFacts:
    data = base.model_dump()
    for key, value in extra.model_dump().items():
        if value not in ("", None):
            data[key] = value
    return EmailFacts(**data)


def extract_facts(parsed: ParsedEmail, *, client=None, use_llm: bool | None = None) -> EmailFacts:
    """Rule-based facts, overlaid by the LLM when enabled; falls back on any LLM error."""
    facts = rule_based_facts(parsed)
    enabled = use_llm if use_llm is not None else bool(config.LLM_API_KEY)
    if not enabled:
        return facts
    try:
        client = client or LLMClient()
        content = client.complete_json(SYSTEM_PROMPT, _build_user_prompt(parsed))
        return _overlay(facts, EmailFacts.model_validate_json(content))
    except Exception as exc:                        # network, bad JSON, schema failure -> rules
        log.warning("LLM extraction failed (%s); using rule-based facts", exc)
        return facts


MAPPING_SYSTEM_PROMPT = (
    "You map the header columns of a carrier rate-deck CSV onto our fixed template. "
    "Reply with ONLY a JSON object whose keys are template column names and whose values are the "
    "EXACT source header strings you were given. Template columns: prefix, destination, inter_rate, "
    "intra_rate, indeterminate_rate, connect_fee, init_duration, increment, grace_time, "
    "effective_date. Include a column only when you are confident; omit anything you are unsure of. "
    "Never invent a header that is not in the list."
)


class ColumnMapping(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prefix: str = ""
    destination: str = ""
    inter_rate: str = ""
    intra_rate: str = ""
    indeterminate_rate: str = ""
    connect_fee: str = ""
    init_duration: str = ""
    increment: str = ""
    grace_time: str = ""
    effective_date: str = ""


def _mapping_prompt(headers, sample_rows=None) -> str:
    header_list = "\n".join(f"- {h}" for h in headers)
    rows = "\n".join(str(row) for row in (sample_rows or []))
    return f"Source headers:\n{header_list}\n\nSample rows:\n{rows}"


def propose_mapping(headers, sample_rows=None, *, client=None,
                    use_llm: bool | None = None) -> dict[str, list[str]]:
    """LLM-proposed ``header_aliases`` (``{template_column: [source_header]}``); ``{}`` if unavailable.

    Proposals are validated against the real headers, so the LLM can never invent a column. Rows are
    never rewritten here - pandas still does the conversion once a human accepts the mapping.
    """
    enabled = use_llm if use_llm is not None else bool(config.LLM_API_KEY)
    if not enabled:
        return {}
    try:
        client = client or LLMClient()
        content = client.complete_json(MAPPING_SYSTEM_PROMPT,
                                       _mapping_prompt(list(headers), sample_rows))
        proposed = ColumnMapping.model_validate_json(content)
    except Exception as exc:                        # network, bad JSON, schema failure -> no suggestion
        log.warning("LLM mapping proposal failed (%s); no suggestion", exc)
        return {}

    by_norm = {_norm(header): str(header) for header in headers}
    aliases: dict[str, list[str]] = {}
    for canonical, header in proposed.model_dump().items():
        header = (header or "").strip()
        if header and _norm(header) in by_norm:
            aliases[canonical] = [by_norm[_norm(header)]]
    return aliases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract facts from a rate-deck email (.eml).")
    parser.add_argument("eml")
    parser.add_argument("--llm", action="store_true", help="use the configured LLM (needs LLM_API_KEY)")
    args = parser.parse_args(argv)

    if args.llm and not config.LLM_API_KEY:
        print("No LLM_API_KEY set - using rule-based extraction.", file=sys.stderr)
    parsed = parse_email(pathlib.Path(args.eml).read_bytes())
    facts = extract_facts(parsed, use_llm=args.llm)
    print(facts.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
