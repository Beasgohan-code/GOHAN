"""Logging configuration.

Plain stdlib logging - no extra dependency - with an optional JSON formatter for
people shipping logs into Loki/CloudWatch. Telegram's own noisy loggers are
turned down so the bot's own output stays readable.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_NOISY = {
    "aiogram.event": logging.WARNING,
    "pyrogram": logging.WARNING,
    "pyrogram.session": logging.WARNING,
    "pyrogram.connection": logging.WARNING,
    "pyrogram.dispatcher": logging.WARNING,
    "aiohttp.access": logging.WARNING,
    "asyncio": logging.WARNING,
}


class HumanFormatter(logging.Formatter):
    """``12:34:56 INFO     gohan.handlers  message``"""

    default_msec_format = "%s.%03d"
    _LEVEL_COLOURS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }

    def __init__(self, *, colour: bool = True) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).astimezone()
        level = record.levelname
        if self.colour:
            colour = self._LEVEL_COLOURS.get(level, "")
            level = f"{colour}{level:<8}\033[0m" if colour else f"{level:<8}"
        else:
            level = f"{level:<8}"
        name = record.name.replace("gohan", "gohan", 1)
        base = f"{ts:%H:%M:%S} {level} {name:<28} {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shippers."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Install the root handler. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter(colour=sys.stderr.isatty()))
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name, noisy_level in _NOISY.items():
        logging.getLogger(name).setLevel(noisy_level)

    # aiohttp's client errors are surfaced by aiogram anyway
    logging.getLogger("aiogram").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Namespace helper so every module logs under ``gohan.*``."""
    return logging.getLogger(name if name.startswith("gohan") else f"gohan.{name}")


__all__ = ["HumanFormatter", "JsonFormatter", "get_logger", "setup_logging"]
