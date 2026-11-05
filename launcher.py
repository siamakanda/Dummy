"""Dummy launcher - double-click ``start.bat`` (or run ``python launcher.py``).

Shows a small terminal menu. Option 1 starts the local dashboard and opens the browser; after that,
everything the operator does happens in the browser. Power users can pass an action directly, e.g.
``python launcher.py status`` / ``sync`` / ``scan`` / ``convert`` / ``dashboard``.
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from store import Store  # noqa: E402  (import after sys.path bootstrap)


def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def banner() -> None:
    print("=" * 58)
    print("  Dummy  -  carrier rate-deck engine")
    print("  (work, unassigned decks, and pushes all happen in the dashboard)")
    print("=" * 58)


def _web_port() -> int:
    return int(os.getenv("DUMMY_PORT", "8090"))


def start_dashboard() -> None:
    import uvicorn
    port = _web_port()
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Dashboard: {url}")
    print("  Opening your browser...   (keep this window open; press Ctrl+C to stop)\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run("engine_web:app", host="127.0.0.1", port=port, log_level="info")
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
    except OSError as exc:
        print(f"\n  Could not start on port {port} ({exc}).")
        print(f"  Something may already be using it - set a different port and retry:")
        print(f'      set DUMMY_PORT=8091   &&   start.bat')


def refresh_trunks() -> None:
    import engine
    from types import SimpleNamespace
    store = Store.open()
    try:
        engine.cmd_sync(store, SimpleNamespace(headed=False))
    finally:
        store.close()


def scan_inbox() -> None:
    import engine
    store = Store.open()
    try:
        scanned, created = engine.scan_inbox(store)
        print(f"\n  Scanned {scanned} file(s); {created} new deck(s).")
    finally:
        store.close()


def convert_mapped() -> None:
    import engine
    store = Store.open()
    try:
        results = engine.run_mapped(store)
        print(f"\n  Converted {len(results)} deck(s)." if results else "\n  No mapped decks to convert.")
    finally:
        store.close()


def show_status() -> None:
    store = Store.open()
    try:
        jobs = store.list_jobs()
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        print(f"\n  Deck files: {len(jobs)}")
        for status, number in sorted(counts.items()):
            print(f"    {status:<16} {number}")
        print(f"  Trunks on the switch: {len(store.all_trunks())}")
    finally:
        store.close()


MENU = [
    ("Start dashboard  (recommended)", start_dashboard),
    ("Refresh trunks from the switch", refresh_trunks),
    ("Scan inbox/ for new deck files", scan_inbox),
    ("Convert mapped decks", convert_mapped),
    ("Show status", show_status),
]

ACTIONS = {"dashboard": start_dashboard, "sync": refresh_trunks, "scan": scan_inbox,
           "convert": convert_mapped, "status": show_status}


def _preflight() -> bool:
    try:
        import fastapi  # noqa: F401
        import pandas  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:
        print(f"\n  Missing dependency: {exc.name}")
        print("  Install them first:   pip install -r requirements.txt")
        print("  (and once:  playwright install chromium )\n")
        return False
    return True


def _pause(message: str = "\n  Press Enter to return to the menu...") -> None:
    try:
        input(message)
    except EOFError:
        raise SystemExit(0)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ACTIONS:
        if not _preflight():
            return 1
        ACTIONS[args[0]]()
        return 0

    if not _preflight():
        input("  Press Enter to close...")
        return 1

    while True:
        clear()
        banner()
        for index, (label, _) in enumerate(MENU, 1):
            print(f"  {index}) {label}")
        print("  0) Exit")
        try:
            choice = input("\n  Choose [1]: ").strip() or "1"
        except EOFError:
            return 0

        if choice == "0":
            print("  Bye.")
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            _pause("  Not a valid choice. Press Enter to return to the menu...")
            continue

        _, action = MENU[int(choice) - 1]
        if action is start_dashboard:
            start_dashboard()          # runs the server until Ctrl+C, then back to the menu
        else:
            action()
            _pause()


if __name__ == "__main__":
    raise SystemExit(main())
