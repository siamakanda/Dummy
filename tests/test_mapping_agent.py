"""Tests for the bounded mapping agent (offline; the LLM is injected)."""
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from mapping_agent import analyze

RAW = pd.DataFrame({"Alpha": ["1201200"], "Beta": ["0.0012"], "Gamma": ["0.0012"]})


def test_analyze_maps_when_proposal_complete():
    result = analyze(RAW, propose=lambda headers, rows, use_llm=None: {
        "prefix": ["Alpha"], "inter_rate": ["Beta"], "intra_rate": ["Gamma"]})

    assert result.status == "mapped"
    assert result.aliases["prefix"] == ["Alpha"]
    assert result.steps == 1


def test_analyze_stops_without_a_suggestion():
    result = analyze(RAW, propose=lambda headers, rows, use_llm=None: {})

    assert result.status == "needs_human"
    assert "prefix" in result.missing


def test_analyze_stops_when_there_is_no_progress():
    result = analyze(RAW, propose=lambda headers, rows, use_llm=None: {"destination": ["Name"]})

    assert result.status == "needs_human"
    assert result.missing


def test_analyze_respects_the_step_budget():
    result = analyze(RAW, max_steps=1,
                     propose=lambda headers, rows, use_llm=None: {"destination": ["Name"]})

    assert result.status == "needs_human"
