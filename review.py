"""Human-in-the-loop: pending queue + trunk matching (AGENTS.md section 5, M9).

Commands:
    python review.py list [--status open]
    python review.py resolve <id> --trunk "NAME"     # choose the trunk for a 'trunk' item
    python review.py resolve <id> --approve          # approve first-time upload / warnings
    python review.py approve-trunk "NAME"
    python review.py match --prefix 6060 [--name ...] [--domain ...]

The internal prompt (`choose_trunk`, `resolve_and_record`) stays injectable for tests.
"""
from __future__ import annotations

import argparse
import json

import config
from store import Store
from trunk_matcher import Candidate, MatchResult, TrunkHints, carrier_from_sender, match_trunks
from upload import find_trunk

MAX_SUGGESTIONS = 8

def _suggestions(match: MatchResult, trunks: list[dict]) -> list[Candidate]:
    """Ranked picks: positive-score candidates first, then other trunks of the sender's carrier."""
    picks: list[Candidate] = []
    seen: set[int] = set()
    for candidate in match.candidates:
        if candidate.score > 0:
            picks.append(candidate)
            seen.add(candidate.trunk_id)
    if match.carrier_id is not None:
        for trunk in trunks:
            trunk_id = int(trunk["trunk_id"])
            if int(trunk["carrier_id"]) == match.carrier_id and trunk_id not in seen:
                picks.append(Candidate(
                    trunk_id=trunk_id, trunk_name=str(trunk.get("trunk_name") or ""),
                    carrier_id=int(trunk["carrier_id"]),
                    carrier_name=str(trunk.get("carrier_name") or ""),
                    score=0.0, reason="same carrier"))
                seen.add(trunk_id)
    return picks[:MAX_SUGGESTIONS]

def choose_trunk(
    match: MatchResult,
    trunks: list[dict],
    *,
    input_fn=input,
    output_fn=print,
) -> int | None:
    """Return a trunk id. A confident match returns immediately; otherwise the human picks."""
    if not match.needs_human and match.trunk_id is not None:
        return match.trunk_id

    why = "ambiguous" if match.ambiguous else ("no match" if match.trunk_id is None else "low confidence")
    output_fn(f"Need a human for the trunk ({why}): {match.reason}")
    picks = _suggestions(match, trunks)
    if not picks:
        output_fn("No candidate trunks to suggest — please resolve manually.")
        return None
    for number, candidate in enumerate(picks, start=1):
        output_fn(f"  {number}. {candidate.trunk_name} [{candidate.carrier_name}] "
                  f"(score {candidate.score:.2f}, {candidate.reason})")
    output_fn("  0. skip")

    answer = str(input_fn("Pick a trunk number (or type a trunk id): ")).strip()
    if not answer or answer == "0":
        return None
    if answer.isdigit():
        number = int(answer)
        if 1 <= number <= len(picks):
            return picks[number - 1].trunk_id
        if any(int(trunk["trunk_id"]) == number for trunk in trunks):
            return number
    output_fn(f"'{answer}' is not a valid choice.")
    return None

def resolve_and_record(
    store: Store,
    run_id: int,
    match: MatchResult,
    trunks: list[dict],
    *,
    input_fn=input,
    output_fn=print,
) -> int | None:
    """Auto-accept a confident match or ask the human; record the outcome as a decision."""
    if not match.needs_human and match.trunk_id is not None:
        store.add_decision(run_id, "trunk_match", f"auto: {match.reason}", str(match.trunk_id))
        return match.trunk_id
    chosen = choose_trunk(match, trunks, input_fn=input_fn, output_fn=output_fn)
    store.add_decision(run_id, "trunk_match", f"human: {match.reason}",
                       "" if chosen is None else str(chosen))
    return chosen

# ---- CLI ------------------------------------------------------------------

def _open_store(path: str) -> Store:
    store = Store(path)
    if not store.all_trunks() and (config.DATA_DIR / "trunks.csv").exists():
        store.load_trunks(config.DATA_DIR / "trunks.csv")
    return store

def _cmd_list(store: Store, args) -> int:
    items = store.list_pending(args.status or None)
    if not items:
        print("No pending items.")
        return 0
    for item in items:
        reasons = json.loads(item.get("reasons") or "[]")
        trunk = store.get_trunk(item["trunk_id"]) if item.get("trunk_id") else None
        label = trunk["trunk_name"] if trunk else "(trunk unresolved)"
        print(f"#{item['id']}  {item['kind']:<8} {item['status']:<9} {label}  "
              f"rows={item.get('rows')} eff={item.get('effective_date') or '-'}")
        if reasons:
            print(f"     reasons: {reasons[:5]}")
    return 0

def _cmd_resolve(store: Store, args) -> int:
    item = store.get_pending(args.id)
    if item is None:
        print(f"No pending item #{args.id}")
        return 2
    trunk_id = item.get("resolved_trunk_id") or item.get("trunk_id")
    if args.trunk:
        trunk = find_trunk(store, args.trunk)
        if trunk is None:
            print(f"Trunk '{args.trunk}' not found")
            return 2
        trunk_id = int(trunk["trunk_id"])
    if trunk_id is None and not args.approve:
        print("Provide --trunk \"NAME\" to choose the trunk, or --approve.")
        return 2
    store.resolve_pending(args.id, trunk_id=trunk_id, approve=args.approve, by=args.by)
    print(f"Resolved pending #{args.id} (trunk_id={trunk_id}, approve={args.approve}). "
          f"The daemon will commit it on its next --commit pass.")
    return 0

def _cmd_approve_trunk(store: Store, args) -> int:
    trunk = find_trunk(store, args.trunk)
    if trunk is None:
        print(f"Trunk '{args.trunk}' not found")
        return 2
    store.approve(int(trunk["trunk_id"]), by=args.by, carrier_id=trunk.get("carrier_id"))
    print(f"Approved trunk '{trunk['trunk_name']}' (id {trunk['trunk_id']}).")
    return 0

def _cmd_match(store: Store, args) -> int:
    trunks = store.all_trunks()
    sender_carrier = carrier_from_sender(args.domain, store) if args.domain else None
    hints = TrunkHints(tech_prefix=args.prefix, trunk_name=args.name, sender_domain=args.domain)
    match = match_trunks(hints, trunks, sender_carrier=sender_carrier)
    print(f"match: {match.trunk_name or '(none)'}  confidence={match.confidence}  "
          f"needs_human={match.needs_human}  ({match.reason})")
    print("chosen trunk id:", choose_trunk(match, trunks))
    return 0

def _cmd_set_domain(store: Store, args) -> int:
    store.set_sender_domain(args.domain, args.carrier_id)
    print(f"Mapped {args.domain} -> carrier {args.carrier_id}")
    return 0

def _cmd_set_aliases(store: Store, args) -> int:
    trunk = find_trunk(store, args.trunk)
    if trunk is None:
        print(f"Trunk '{args.trunk}' not found")
        return 2
    try:
        mapping = json.loads(args.mapping)
        if not isinstance(mapping, dict):
            raise ValueError
    except ValueError:
        print('mapping must be a JSON object, e.g. {"prefix": ["mydial"]}')
        return 2
    store.set_config(int(trunk["trunk_id"]), carrier_id=trunk.get("carrier_id"),
                     header_aliases=mapping)
    print(f"Saved header_aliases for '{trunk['trunk_name']}'.")
    return 0

def _suggested_aliases(item: dict) -> dict:
    for reason in json.loads(item.get("reasons") or "[]"):
        prefix = "suggested_aliases: "
        if isinstance(reason, str) and reason.startswith(prefix):
            try:
                mapping = json.loads(reason[len(prefix):])
            except ValueError:
                return {}
            return mapping if isinstance(mapping, dict) else {}
    return {}

def _cmd_apply_mapping(store: Store, args) -> int:
    item = store.get_pending(args.id)
    if item is None:
        print(f"No pending item #{args.id}")
        return 2
    if item.get("kind") != "mapping":
        print(f"Pending #{args.id} is kind '{item.get('kind')}', not 'mapping'")
        return 2
    trunk_id = item.get("trunk_id") or item.get("resolved_trunk_id")
    if not trunk_id:
        print("This mapping item has no trunk; resolve the trunk first.")
        return 2
    if args.aliases:
        try:
            mapping = json.loads(args.aliases)
            if not isinstance(mapping, dict):
                raise ValueError
        except ValueError:
            print('--aliases must be a JSON object, e.g. {"prefix": ["Dialcode"]}')
            return 2
    else:
        mapping = _suggested_aliases(item)
        if not mapping:
            print("No suggested mapping on this item; pass --aliases '{...}'.")
            return 2
    trunk = store.get_trunk(int(trunk_id))
    store.set_config(int(trunk_id), carrier_id=trunk.get("carrier_id") if trunk else None,
                     header_aliases=mapping)
    store.set_pending_status(args.id, "applied")
    print(f"Saved header_aliases for trunk {trunk_id}: {json.dumps(mapping)}")
    print(f"Marked pending #{args.id} applied; re-run the source (or the daemon) to convert + upload.")
    return 0

def _cmd_add_alias(store: Store, args) -> int:
    trunk = find_trunk(store, args.trunk)
    if trunk is None:
        print(f"Trunk '{args.trunk}' not found")
        return 2
    cfg = store.get_config(int(trunk["trunk_id"])) or {}
    aliases = list(cfg.get("name_aliases") or [])
    if args.alias not in aliases:
        aliases.append(args.alias)
    store.set_config(int(trunk["trunk_id"]), carrier_id=trunk.get("carrier_id"),
                     name_aliases=aliases)
    print(f"Aliases for '{trunk['trunk_name']}': {aliases}")
    return 0

def _cmd_config(store: Store, args) -> int:
    if args.trunk:
        trunk = find_trunk(store, args.trunk)
        if trunk is None:
            print(f"Trunk '{args.trunk}' not found")
            return 2
        cfg = store.get_config(int(trunk["trunk_id"])) or {}
        print(json.dumps({"trunk": trunk["trunk_name"], "config": cfg}, indent=2, default=str))
        return 0
    print("sender domains:")
    for row in store.list_sender_domains():
        print(f"  {row['domain']} -> carrier {row['carrier_id']}")
    approved = [t["trunk_name"] for t in store.all_trunks() if store.is_approved(int(t["trunk_id"]))]
    print("approved trunks:", approved)
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dummy human-in-the-loop CLI.")
    parser.add_argument("--db", default=str(config.DB_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show pending items")
    p_list.add_argument("--status", default="open")

    p_resolve = sub.add_parser("resolve", help="resolve a pending item")
    p_resolve.add_argument("id", type=int)
    p_resolve.add_argument("--trunk", help="choose the trunk by name")
    p_resolve.add_argument("--approve", action="store_true", help="approve first-time upload / warnings")
    p_resolve.add_argument("--by", default="human")

    p_approve = sub.add_parser("approve-trunk", help="approve a trunk for auto-upload")
    p_approve.add_argument("trunk")
    p_approve.add_argument("--by", default="human")

    p_match = sub.add_parser("match", help="manually run the trunk matcher")
    p_match.add_argument("--prefix", default="")
    p_match.add_argument("--name", default="")
    p_match.add_argument("--domain", default="")

    p_domain = sub.add_parser("set-domain", help="map a sender domain to a carrier")
    p_domain.add_argument("domain")
    p_domain.add_argument("carrier_id", type=int)

    p_aliases = sub.add_parser("set-aliases", help="set header_aliases (JSON) for a trunk")
    p_aliases.add_argument("trunk")
    p_aliases.add_argument("mapping", help='JSON object, e.g. {"prefix": ["mydial"]}')

    p_mapping = sub.add_parser("apply-mapping",
                               help="apply a (suggested) column mapping and mark the item applied")
    p_mapping.add_argument("id", type=int)
    p_mapping.add_argument("--aliases", help='override JSON, e.g. {"prefix": ["Dialcode"]}')

    p_alias = sub.add_parser("add-alias", help="add a trunk name alias used to match decks")
    p_alias.add_argument("trunk")
    p_alias.add_argument("alias", help='e.g. "BTCL TNT Domestic"')

    p_config = sub.add_parser("config", help="show sender domains / per-trunk config")
    p_config.add_argument("--trunk")

    args = parser.parse_args(argv)
    with _open_store(args.db) as store:
        handler = {"list": _cmd_list, "resolve": _cmd_resolve,
                   "approve-trunk": _cmd_approve_trunk, "match": _cmd_match,
                   "set-domain": _cmd_set_domain, "set-aliases": _cmd_set_aliases,
                   "apply-mapping": _cmd_apply_mapping, "add-alias": _cmd_add_alias,
                   "config": _cmd_config}[args.command]
        return handler(store, args)

if __name__ == "__main__":
    raise SystemExit(main())
