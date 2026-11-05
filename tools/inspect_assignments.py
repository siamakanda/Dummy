"""Capture the portal's rate-deck ASSIGNMENT network calls (read-only).

Run this headed once, then send `data/snapshots/assignments.json` (and `scripts/`) so we can
implement assigning a new deck to a trunk without guessing the endpoint.

    python tools/inspect_assignments.py --headed
    python tools/inspect_assignments.py --deck-id 178 --headed
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config                      # noqa: E402
from portal import Portal          # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture rate-deck assignment network calls.")
    parser.add_argument("--deck-id", type=int, help="deck whose 'Assigned To' badge to open")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args(argv)

    records = []
    out = config.SNAP_DIR / "assignments.json"
    scripts_dir = config.SNAP_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    with Portal(headless=not args.headed) as portal:
        def on_request(request):
            if request.resource_type in ("xhr", "fetch", "document"):
                records.append({
                    "method": request.method,
                    "url": request.url,
                    "resource": request.resource_type,
                    "post_data": request.post_data,
                })

        portal.page.on("request", on_request)
        portal.goto("rate_decks.php")
        scripts = portal.page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")

        selector = (f"button.assigned-badge[onclick*='toggleAssignments({args.deck_id})']"
                    if args.deck_id else "button.assigned-badge")
        if portal.page.locator(selector).count():
            portal.page.locator(selector).first.click()
            portal.page.wait_for_timeout(1500)
        else:
            print("no 'Assigned To' badge found on the page")

        portal.snapshot("assignments")

        for src in scripts:
            if src.startswith("http") and "instacall" not in src and "switchportal" not in src:
                continue  # skip CDNs
            try:
                response = portal.ctx.request.get(src)
                name = src.split("?")[0].rstrip("/").split("/")[-1] or "script.js"
                (scripts_dir / name).write_text(response.text(), encoding="utf-8")
            except Exception as exc:      # pragma: no cover - live tool
                print("script fetch failed:", src, exc)

    out.write_text(json.dumps({"requests": records, "scripts": scripts}, indent=2), encoding="utf-8")
    print(f"saved {len(records)} request(s) and {len(scripts)} script ref(s) -> {out}")
    print("grep the requests/scripts for 'assign' to find the endpoint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
