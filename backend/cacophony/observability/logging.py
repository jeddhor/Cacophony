"""Structured logging (design document sections 86 and 87).

Section 86 lists the fields every application log line should carry::

    timestamp  run_id  job_id  provider  entity  record_range
    duration   status  error

So a log line is a mapping, not a sentence. It is rendered as JSON when
something will parse it and as readable text when a person is watching, and the
fields are the same either way.

Section 87 governs debug material. Prompts and raw model output may contain
whatever the user put in their schema, so they are only ever logged when debug
logging is explicitly turned on - never at the default level, and never merely
because an error occurred.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC
from typing import Any

__all__ = [
    "JsonFormatter",
    "RunLogger",
    "TextFormatter",
    "configure_logging",
    "get_logger",
]

LOGGER_NAME = "cacophony"

#: Fields section 86 asks for, in the order a reader wants them.
_ORDERED_FIELDS = (
    "run_id",
    "job_id",
    "entity",
    "provider",
    "record_range",
    "duration_ms",
    "status",
    "error",
)

#: Attributes ``logging`` puts on every record, which are not ours.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for anything that ingests logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _iso(record.created),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Readable lines that still carry every structured field."""

    def format(self, record: logging.LogRecord) -> str:
        extras = _extras(record)
        ordered = [
            f"{key}={extras.pop(key)}" for key in _ORDERED_FIELDS if extras.get(key) is not None
        ]
        ordered.extend(f"{key}={value}" for key, value in sorted(extras.items()))
        suffix = ("  " + " ".join(ordered)) if ordered else ""
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        line = f"{stamp} {record.levelname.lower():<7} {record.getMessage()}{suffix}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _STANDARD and not key.startswith("_") and value is not None
    }


def _iso(created: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(created, tz=UTC).isoformat()


def configure_logging(
    level: str = "warning",
    *,
    fmt: str = "text",
    stream: Any = None,
) -> logging.Logger:
    """Set up Cacophony's logger. Idempotent, so calling it twice is safe."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.WARNING))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


class RunLogger:
    """A logger that carries a run's identity on every line.

    Constructed once per run so that ``run_id`` and ``entity`` do not have to
    be repeated at each call site - which is how they end up missing from the
    one line that mattered.
    """

    def __init__(
        self, run_id: str, *, entity: str | None = None, debug_payloads: bool = False
    ) -> None:
        self.run_id = run_id
        self.entity = entity
        #: Section 87: prompts and raw responses are withheld unless asked for.
        self.debug_payloads = debug_payloads
        self._logger = get_logger("run")

    def bind(self, **context: Any) -> RunLogger:
        bound = RunLogger(
            self.run_id,
            entity=context.get("entity", self.entity),
            debug_payloads=self.debug_payloads,
        )
        return bound

    def _emit(self, level: int, message: str, **fields: Any) -> None:
        payload = {"run_id": self.run_id}
        if self.entity is not None:
            payload["entity"] = self.entity
        payload.update({key: value for key, value in fields.items() if value is not None})
        self._logger.log(level, message, extra=payload)

    def debug(self, message: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit(logging.WARNING, message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit(logging.ERROR, message, **fields)

    def payload(self, message: str, **fields: Any) -> None:
        """Log prompts or raw provider output (design document section 87).

        These may contain sensitive user-supplied content, so they go nowhere
        unless debug payloads were explicitly enabled for this run.
        """
        if self.debug_payloads:
            self._emit(logging.DEBUG, message, **fields)

    def batch(
        self,
        *,
        entity: str,
        first: int,
        last: int,
        duration_ms: float,
        status: str = "ok",
        **fields: Any,
    ) -> None:
        """Section 86's canonical line: an entity, a record range, a duration."""
        self._emit(
            logging.DEBUG,
            "batch written",
            entity=entity,
            record_range=f"{first}-{last}",
            duration_ms=round(duration_ms, 2),
            status=status,
            **fields,
        )
