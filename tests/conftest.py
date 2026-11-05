"""Test-wide guards: keep the suite offline regardless of the developer's .env.

A real ``DEEPSEEK_LLM`` / ``LLM_API_KEY`` or mail server in ``.env`` must not make tests hit the network,
so we blank those settings for every test (individual tests pass their own fakes instead).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pytest

import config


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "IMAP_HOST", "")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "IMAP_MARK_SEEN", True)
