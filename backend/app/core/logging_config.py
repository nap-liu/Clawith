"""Centralized logging configuration using loguru."""

import os
import sys
import logging
from contextvars import ContextVar
from typing import Optional

from loguru import logger

# Context variable for trace ID
from uuid import uuid4

trace_id_var: ContextVar[str] = ContextVar("trace_id", default=None)


NOISY_CONNECTION_LOGGERS = {
    # WebSocket accepted / HTTP access lines from uvicorn.
    "uvicorn.access": logging.WARNING,
    # "connection open" / "connection closed" emitted by websockets.
    "websockets": logging.WARNING,
    "websockets.server": logging.WARNING,
    "websockets.client": logging.WARNING,
    "uvicorn.protocols.websockets.websockets_impl": logging.WARNING,
}


def get_trace_id() -> str:
    """Get current trace ID from context."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Set trace ID in context."""
    trace_id_var.set(trace_id)


def configure_logging():
    """Configure loguru with custom format including trace ID."""
    # Remove default handler
    logger.remove()

    # Add stdout handler with custom format and filter to ensure trace_id exists
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>{extra[trace_id]:-<12}</cyan> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        enqueue=True,
        backtrace=True,
        diagnose=True,
        filter=lambda record: (record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4())) is not None)
    )

    # Persistent rotating file sink. Written to LOG_DIR (bind-mounted to the
    # host in compose) so logs SURVIVE container rebuilds/deploys — that's the
    # whole point: being able to investigate an incident after the fact. When
    # LOG_DIR is unset (tests / ad-hoc one-shot containers) the file sink is
    # skipped so nothing writes to a throwaway path. Best-effort: a broken file
    # sink must NEVER block app startup.
    log_dir = os.environ.get("LOG_DIR", "").strip()
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            logger.add(
                os.path.join(log_dir, "clawith.log"),
                level=os.environ.get("LOG_FILE_LEVEL", "INFO"),
                # No ANSI colour tags — written verbatim to disk.
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[trace_id]:-<12} | {name}:{line} - {message}",
                rotation=os.environ.get("LOG_ROTATION", "100 MB"),
                retention=os.environ.get("LOG_RETENTION", "14 days"),
                compression="gz",
                enqueue=True,        # async — never block the request path
                backtrace=True,
                diagnose=False,      # do NOT persist local variables (may hold secrets)
                filter=lambda record: (record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4())) is not None),
            )
        except Exception as _e:
            # Logging setup must never crash the app; fall back to stdout only.
            logger.warning(f"[logging] persistent file sink at {log_dir!r} disabled (setup failed): {_e}")

    return logger


def quiet_noisy_connection_loggers() -> None:
    """Reduce chatty transport-level logs while keeping warnings/errors visible."""
    for logger_name, level in NOISY_CONNECTION_LOGGERS.items():
        target = logging.getLogger(logger_name)
        target.setLevel(level)


def intercept_standard_logging():
    """Redirect standard library logging to loguru."""
    class InterceptHandler(logging.Handler):
        def emit(self, record):
            # Get corresponding loguru level
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find the caller's frame
            frame, depth = logging.currentframe(), 2
            while frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1

            # Capture the message safely
            try:
                message = record.getMessage()
            except (TypeError, ValueError):
                # Fallback if formatting fails (e.g. third party lib bug)
                if record.args:
                    message = f"{record.msg} [args={record.args}]"
                else:
                    message = record.msg

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, message
            )

    # Replace all standard logger handlers
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).handlers = [InterceptHandler()]
        logging.getLogger(name).propagate = False
    quiet_noisy_connection_loggers()


# Configure on import
logger = configure_logging()
