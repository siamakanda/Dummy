import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pytest

from retry import retry_call


def test_retry_succeeds_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    assert retry_call(flaky, attempts=3, base_delay=0) == "ok"
    assert calls["n"] == 3


def test_retry_gives_up():
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        retry_call(always, attempts=2, base_delay=0)
    assert calls["n"] == 2


def test_retry_does_not_catch_unlisted_exceptions():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        retry_call(boom, attempts=3, base_delay=0, exceptions=(ValueError,))
    assert calls["n"] == 1
