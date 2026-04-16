"""Explicit error classes for CLI-tool execution.

Per spec §9. Strings used here are also the values returned to the caller
and surfaced in structured logs; do not rename without updating the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CliToolErrorClass(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    BINARY_FAILED = "BINARY_FAILED"
    SANDBOX_FAILED = "SANDBOX_FAILED"


@dataclass
class CliToolError(Exception):
    error_class: CliToolErrorClass
    message: str

    def __str__(self) -> str:
        return f"[{self.error_class.value}] {self.message}"
