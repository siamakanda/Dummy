"""Tests for the launcher (dashboard-only; no server started)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import launcher

ROOT = pathlib.Path(__file__).parent.parent


def test_start_bat_exists():
    assert (ROOT / "start.bat").exists()


def test_default_port():
    assert launcher._web_port() == 8090


def test_preflight_ok():
    assert launcher._preflight() is True
