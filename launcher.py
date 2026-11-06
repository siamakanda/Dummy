"""Dummy launcher - double-click ``start.bat`` (or run ``python launcher.py``).

Opens the local dashboard in the browser. Every option lives in the dashboard; the terminal is just the
engine's console (keep it open, Ctrl+C to stop).
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def _web_port() -> int:
    return int(os.getenv("DUMMY_PORT", "8090"))


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


def start_dashboard() -> None:
    import uvicorn
    port = _web_port()
    url = f"http://127.0.0.1:{port}"
    print("=" * 58)
    print("  Dummy  -  carrier rate-deck engine")
    print(f"  Dashboard: {url}")
    print("  Opening your browser...  (keep this window open; Ctrl+C to stop)")
    print("=" * 58)
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run("engine_web:app", host="127.0.0.1", port=port, log_level="info")
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
    except OSError as exc:
        print(f"\n  Could not start on port {port} ({exc}).")
        print("  Something may be using it - set a different port and retry:")
        print("      set DUMMY_PORT=8091   &&   start.bat")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not _preflight():
        input("  Press Enter to close...")
        return 1
    start_dashboard()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
