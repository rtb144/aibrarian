"""Structured JSON logging for every service (one JSON object per line)."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "aibrarian"):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "service": self.service,
            "msg": record.getMessage(),
        }
        # anything passed via logger.info(..., extra={...})
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


_SECRET_PARAM = re.compile(r"(?i)\b(key|api_key|apikey|access_token|token)=([^&\s\"']+)")


def redact(text: str) -> str:
    """Mask API keys in URLs / query strings (``?key=AIza...`` -> ``?key=***``)."""
    return _SECRET_PARAM.sub(r"\1=***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        return True


def setup_logging(service: str, level: str = "INFO", json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service) if json_logs
                         else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    handler.addFilter(RedactFilter())
    root.handlers[:] = [handler]
    # httpx/httpcore log every request URL at INFO (incl. query-string API keys)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    root.setLevel(level.upper())
