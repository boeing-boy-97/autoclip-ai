"""Structured logging setup with redaction (see docs/LOGGING.md)."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

# Keys whose values must never reach a log sink.
REDACT_KEY_PATTERN = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|"
    r"refresh_token|access_token|client_secret|pepper|signature)"
)
SIGNED_URL_QUERY = re.compile(r"(\?|&)(X-Amz-[A-Za-z]+|Signature|sig|token)=[^&\s]*")
EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")

REDACTED = "***"


def _redact_value(key: str, value: Any) -> Any:
    if REDACT_KEY_PATTERN.search(key):
        return REDACTED
    if isinstance(value, str):
        value = SIGNED_URL_QUERY.sub(r"\1\2=REDACTED", value)
        return EMAIL.sub(r"\1***\2", value)
    return value


def redaction_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: redact sensitive keys and scrub signed URLs / emails in strings."""
    for key in list(event_dict.keys()):
        event_dict[key] = _redact_value(key, event_dict[key])
    return event_dict


def configure_logging(*, level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        redaction_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    # Third-party noise control.
    for noisy, noisy_level in {
        "uvicorn.access": logging.WARNING,
        "sqlalchemy.engine": logging.WARNING,
        "urllib3": logging.WARNING,
        "httpx": logging.WARNING,
        "googleapiclient": logging.WARNING,
        "celery.app.trace": logging.INFO,
    }.items():
        logging.getLogger(noisy).setLevel(noisy_level)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
