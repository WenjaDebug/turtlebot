"""Structured logging.

Two sinks are configured:

* Console: human-readable one-liners (or JSON with ``TURTLE_LOG__JSON_CONSOLE=true``).
* ``var/log/turtlebot.jsonl``: one JSON object per record, rotated by size. Structured fields
  passed via ``extra={"ctx": {...}}`` are merged into the object, which makes the log greppable
  with ``jq`` (e.g. ``jq 'select(.event=="order.placed")'``).

Order events additionally go to ``var/log/orders.jsonl`` through the ``turtlebot.orders``
logger, which gives a complete, append-only audit trail of everything sent to the broker.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from turtlebot.config import LogSettings

ORDER_LOGGER: Final = "turtlebot.orders"
_RESERVED: Final = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

# Third-party loggers that are noisy at INFO.
_QUIET: Final = {
    "ib_async": logging.WARNING,
    "ib_async.wrapper": logging.CRITICAL,  # TWS errors are logged by the adapter itself
    "ib_async.client": logging.WARNING,
    "yfinance": logging.CRITICAL,
    "peewee": logging.WARNING,
    "urllib3": logging.WARNING,
}


class JsonFormatter(logging.Formatter):
    """Render records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102 - inherited
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, Mapping):
            payload.update(ctx)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "ctx" and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Compact human-readable format with structured context appended as ``key=value``."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102 - inherited
        line = super().format(record)
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, Mapping) and ctx:
            line += "  " + " ".join(f"{k}={v}" for k, v in ctx.items())
        return line


def configure_logging(settings: LogSettings, log_dir: Path | None) -> None:
    """Configure the root logger.

    Args:
        settings: Logging settings.
        log_dir: Directory for the JSON log files; ``None`` logs to the console only.
    """
    root = logging.getLogger()
    orders = logging.getLogger(ORDER_LOGGER)
    for logger in (root, orders):  # idempotent: close handlers of a previous configuration
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    root.setLevel(settings.level)

    console = logging.StreamHandler()
    console.setFormatter(JsonFormatter() if settings.json_console else ConsoleFormatter())
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        main_file = logging.handlers.RotatingFileHandler(
            log_dir / "turtlebot.jsonl",
            maxBytes=settings.max_bytes,
            backupCount=settings.backup_count,
            encoding="utf-8",
        )
        main_file.setFormatter(JsonFormatter())
        root.addHandler(main_file)

        orders_file = logging.handlers.RotatingFileHandler(
            log_dir / "orders.jsonl",
            maxBytes=settings.max_bytes,
            backupCount=settings.backup_count,
            encoding="utf-8",
        )
        orders_file.setFormatter(JsonFormatter())
        orders_file.setLevel(logging.DEBUG)
        orders.addHandler(orders_file)

    for name, level in _QUIET.items():
        logging.getLogger(name).setLevel(level)


def log_order_event(event: str, message: str, **ctx: object) -> None:
    """Log an order lifecycle event to the order audit log (and the main log).

    Args:
        event: Machine-readable event name, e.g. ``order.placed`` or ``order.filled``.
        message: Human-readable description.
        **ctx: Structured context (symbol, order_id, qty, price, ...).
    """
    logging.getLogger(ORDER_LOGGER).info(message, extra={"ctx": {"event": event, **ctx}})
