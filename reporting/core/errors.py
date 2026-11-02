"""Typed exceptions: the only errors allowed to cross a module boundary.

The HTTP layer maps each class to a status code (see ``reporting.app.errors``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Issue:
    """One validation problem, located by a JSON path into the request (``columns[3].expr``)."""

    path: str
    message: str
    code: str = "invalid"


class ReportingError(Exception):
    """Base class for all boundary-crossing errors."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(ReportingError):
    pass


class PermissionDenied(ReportingError):
    pass


class ConnectionFailed(ReportingError):
    pass


class QueryTimeout(ReportingError):
    pass


class Conflict(ReportingError):
    """The resource changed since it was read (optimistic concurrency, HTTP 409)."""


class ValidationFailed(ReportingError):
    def __init__(
        self,
        message: str,
        issues: list[Issue] | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.issues = issues or [Issue(path="", message=message)]


class StaleSnapshot(ValidationFailed):
    """The spec was built against an older schema snapshot (mapped to HTTP 409)."""
