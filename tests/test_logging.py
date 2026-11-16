import json
import logging
import time
from datetime import datetime, timezone

from app.logging_setup import JsonFormatter


def test_json_formatter_ts_is_utc_with_millis(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello", (), None)
        record.created = datetime(2026, 10, 31, 14, 58, 5, 123000, tzinfo=timezone.utc).timestamp()
        payload = json.loads(JsonFormatter().format(record))
        assert payload["ts"] == "2026-10-31T14:58:05.123Z"
        assert payload["ts"].endswith("Z")
    finally:
        monkeypatch.undo()
        if hasattr(time, "tzset"):
            time.tzset()
