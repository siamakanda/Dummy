"""One-shot logging configuration for the CLI/daemon (M13)."""
from __future__ import annotations

import logging
import sys

import config


def configure(level: str | None = None, log_file: str | None = None) -> None:
    """Configure root logging: timestamped console output plus an optional file."""
    resolved = getattr(logging, (level or config.LOG_LEVEL).upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    path = config.LOG_FILE if log_file is None else log_file
    if path:
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=resolved,
        handlers=handlers,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("dummy").setLevel(resolved)
