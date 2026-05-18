"""Console logging configuration.

The CLI logs to stderr so command output on stdout stays clean and pipe-able.
The log level can be raised via the `TIMECLOCK_LOG` env var (e.g. `DEBUG`).
"""

from __future__ import annotations

import logging
import os
import sys


_DEFAULT_LEVEL = "INFO"


def configure_logging(verbose: bool = False) -> logging.Logger:
    """Configure root logging and return the package logger.

    Precedence (highest wins): ``verbose`` flag → ``TIMECLOCK_LOG`` env var → INFO.
    """
    level_name = "DEBUG" if verbose else os.environ.get("TIMECLOCK_LOG", _DEFAULT_LEVEL)
    level = getattr(logging, level_name.upper(), logging.INFO)

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
