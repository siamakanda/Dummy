"""Tests for the double-click launcher (offline; no server started)."""
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import config
import launcher
from store import Store

ROOT = pathlib.Path(__file__).parent.parent


def test_actions_are_registered():
    assert set(launcher.ACTIONS) == {"dashboard", "sync", "scan", "convert", "status"}
    assert len(launcher.MENU) == 5


def test_start_bat_exists():
    assert (ROOT / "start.bat").exists()


def test_show_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "dummy.db")
    store = Store(tmp_path / "dummy.db")
    store.add_job("file:x", file_path="x.csv", file_name="x.csv")
    store.close()

    launcher.show_status()

    out = capsys.readouterr().out
    assert "Deck files: 1" in out
    assert "unmapped" in out


def test_main_exits_cleanly_on_eof(monkeypatch):
    monkeypatch.setattr(launcher, "clear", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    assert launcher.main([]) == 0
