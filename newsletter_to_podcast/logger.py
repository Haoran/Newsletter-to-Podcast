from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload: Dict[str, Any] = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        # Include any custom attributes set via logging `extra={...}`
        exclude = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "created",
        }
        for k, v in record.__dict__.items():
            if k not in exclude and k not in payload and not k.startswith("_"):
                try:
                    json.dumps(v)  # check serializable
                    payload[k] = v
                except Exception:
                    payload[k] = str(v)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(lvl)
    handler.setFormatter(JsonFormatter())
    root.handlers.clear()
    root.addHandler(handler)


def gha_notice(level: str, message: str) -> None:
    # Emit GitHub Actions annotations for quick visibility
    prefix = {
        "ERROR": "::error::",
        "WARNING": "::warning::",
        "NOTICE": "::notice::",
    }.get(level.upper())
    if prefix:
        print(f"{prefix}{message}")
