"""Small retry/backoff helper for transient network calls (M13)."""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

log = logging.getLogger("dummy.retry")

T = TypeVar("T")


def retry_call(fn: Callable[..., T], *args, attempts: int = 3, base_delay: float = 0.3,
               backoff: float = 2.0, exceptions: tuple[type[Exception], ...] = (Exception,),
               **kwargs) -> T:
    """Call ``fn`` up to ``attempts`` times, sleeping ``base_delay * backoff**n`` between tries.

    Only exceptions listed in ``exceptions`` are retried; anything else propagates immediately.
    """
    delay = base_delay
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except exceptions as exc:  # noqa: PERF203
            last = exc
            if attempt >= attempts:
                break
            log.warning("retry %d/%d for %s: %s", attempt, attempts,
                        getattr(fn, "__name__", fn), exc)
            time.sleep(delay)
            delay *= backoff
    raise last  # type: ignore[misc]
