import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any

# Attributes present on every LogRecord; anything else was passed via `extra=` and is
# emitted as a top-level JSON field. `color_message` is uvicorn's ANSI-colored duplicate.
_RESERVED_ATTRS = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                log[key] = value
        if record.exc_info:
            log["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log, default=str)


def setup_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JSONFormatter}},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["stdout"], "level": level.upper()},
            "loggers": {
                # Route uvicorn through the JSON handler; request logging is done by our
                # middleware, so uvicorn's own access log is silenced.
                "uvicorn": {"handlers": ["stdout"], "level": level.upper(), "propagate": False},
                "uvicorn.error": {"handlers": ["stdout"], "level": level.upper(), "propagate": False},
                "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
                "sqlalchemy.engine": {"level": "WARNING"},
                "passlib": {"level": "ERROR"},
            },
        }
    )
