"""Console logging configuration.

The CLI logs to stderr so command output on stdout stays clean and pipe-able.
By default we log at WARNING — quiet runs, only errors and surprises surface.
Pass ``-v`` for INFO-level progress messages, or set ``TIMECLOCK_LOG=DEBUG``
for the full request/response detail.
"""

from __future__ import annotations

import logging
import os
import sys


_DEFAULT_LEVEL = "WARNING"


def configure_logging(verbose: bool = False) -> logging.Logger:
    """Configure root logging and return the package logger.

    Precedence (highest wins): ``verbose`` flag → ``TIMECLOCK_LOG`` env var → WARNING.
    With ``verbose=True`` we elevate to INFO (not DEBUG) — DEBUG is reserved
    for the env var so casual ``-v`` users don't drown in urllib3 chatter.
    """
    level_name = "INFO" if verbose else os.environ.get("TIMECLOCK_LOG", _DEFAULT_LEVEL)
    level = getattr(logging, level_name.upper(), logging.WARNING)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    root = logging.getLogger()
    # Avoid duplicate handlers if main() is called twice in the same process.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Tame noisy third-party loggers unless we're in DEBUG mode.
    if level > logging.DEBUG:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)

    return logging.getLogger("timeclock_cli")
