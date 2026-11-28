"""The app's own log: logs/app.log in the results folder, rotated at 1 MB (three old copies kept).
Backend events (tests, errors, updates) and the window's script errors both go here."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

NAME = "iroptimizer"
_handler: RotatingFileHandler | None = None


def setup(out_dir: Path) -> logging.Logger:
    global _handler
    log = logging.getLogger(NAME)
    want = (Path(out_dir) / "logs" / "app.log").resolve()
    if _handler is not None and Path(_handler.baseFilename).resolve() != want:
        log.removeHandler(_handler)
        _handler.close()
        _handler = None
    if _handler is None:
        try:
            d = Path(out_dir) / "logs"
            d.mkdir(parents=True, exist_ok=True)
            _handler = RotatingFileHandler(d / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(_handler)
            log.setLevel(logging.DEBUG)  # polls are logged at DEBUG: 1 MB holds several hours of tests
        except OSError:
            pass
    return log


def get() -> logging.Logger:
    return logging.getLogger(NAME)
