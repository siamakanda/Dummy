"""Pure HTML parsing (no browser) so it can be unit-tested and tuned from saved snapshots.

Portal-specific knowledge lives in portal.py; everything here works on a raw HTML string.
"""
import re
from bs4 import BeautifulSoup

def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()

def parse_tables(html: str) -> list[dict]:
    """Return [{'headers': [...], 'rows': [{'cells': [...], 'links': [...]}]}] for every <table>."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for t in soup.find_all("table"):
        trs = t.find_all("tr")
        if not trs:
            continue
        headers, body = [], []
        for tr in trs:
            ths = tr.find_all("th")
            tds = tr.find_all("td")
            if ths and not tds and not headers:
                headers = [_txt(h) for h in ths]
            elif tds:
                body.append({
                    "cells": [_txt(c) for c in tds],
                    "links": [a.get("href", "") for a in tr.find_all("a") if a.get("href")],
                })
        if body:
            out.append({"headers": headers, "rows": body})
    return out

def carrier_name(html: str, fallback: str) -> str:
    """The carrier name is the page <h1> (the <title> carries no name)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in ("h1", "h2", "h3"):
        el = soup.find(tag)
        if el and _txt(el):
            return _txt(el)
    return fallback

def extract_carriers(html: str) -> list[dict]:
    """Carrier ids from the /carriers page (boxes link to /carrier_trunks.php?id=N)."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[int] = set()
    out: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "carrier_trunks" not in href or "edit=" in href or "add=" in href:
            continue
        m = re.search(r"[?&]id=(\d+)", href)
        if not m:
            continue
        cid = int(m.group(1))
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"carrier_id": cid, "trunks_path": href})
    out.sort(key=lambda c: c["carrier_id"])
    return out

def extract_trunks(html: str) -> list[dict]:
    """One dict per trunk card (div.cpanel) on a carrier page."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for card in soup.select("div.cpanel"):
        head = card.select_one(".cpanel-head")
        if head is None:
            continue
        strong = head.find("strong")
        name = _txt(strong) if strong else ""
        if not name:
            continue
        head_text = _txt(head)

        trunk_id = ""
        for a in head.find_all("a", href=True):
            m = re.search(r"[?&]edit=(\d+)", a["href"])
            if m:
                trunk_id = m.group(1)
                break

        m = re.search(r"prefix:\s*([0-9A-Za-z._-]+)", head_text)
        prefix = m.group(1) if m else ""

        status = ""
        for badge in head.select(".cbadge"):
            text = _txt(badge)
            if text in ("Active", "Disabled", "Inactive"):
                status = text
                break

        deck = head.select_one(".cost-deck")
        cost_deck = _txt(deck) if deck else ""

        results.append({
            "trunk_name": name,
            "trunk_id": trunk_id,
            "prefix": prefix,
            "status": status,
            "cost_deck": cost_deck,
        })
    return results

def extract_decks(html: str) -> list[dict]:
    """Rate decks from the /rate_decks page cards (div.rd-card)."""
    soup = BeautifulSoup(html, "html.parser")
    decks = []
    for card in soup.select("div.rd-card"):
        m = re.match(r"rd-card-(\d+)", card.get("id", ""))
        if not m:
            continue
        name_el = card.select_one(".rd-name")
        type_el = card.select_one(".rd-type-badge")
        mode_el = card.select_one(".rd-mode-badge")
        status_el = card.select_one(".rd-status")
        decks.append({
            "deck_id": int(m.group(1)),
            "name": _txt(name_el) if name_el else "",
            "type": _txt(type_el) if type_el else "",
            "mode": _txt(mode_el) if mode_el else "",
            "status": _txt(status_el) if status_el else "",
        })
    return decks
