"""Structured logging: JSON lines to a rotating file, readable text to stdout.

A redaction filter on every handler removes Telegram bot tokens from log
records, whatever logger produced them.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")


def redact(text: str) -> str:
    return _TOKEN_RE.sub("<redacted-token>", text)


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS and not k.startswith("_")}


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        for key, value in _extras(record).items():
            if isinstance(value, str):
                setattr(record, key, redact(value))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
        ts = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
        data: dict[str, object] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data.update({k: v if isinstance(v, (int, float, str, bool, type(None))) else str(v)
                     for k, v in _extras(record).items()})
        if record.exc_info:
            data["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        extras = _extras(record)
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(TextFormatter())
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    for handler in (console, file_handler):
        handler.addFilter(RedactFilter())
        root.addHandler(handler)

    # httpx logs full request URLs (which contain the bot token) at INFO.
    for noisy in ("httpx", "httpcore", "peewee", "uvicorn.access", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
