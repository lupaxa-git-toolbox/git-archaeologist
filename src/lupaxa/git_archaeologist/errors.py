"""Coded failures for the command line and analysis."""

from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """A failure with a stable code and process exit status."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.context = context or {}


class SectionError(ToolError):
    """An optional analysis section failed and a partial report is still possible."""

    def __init__(self, code: str, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, 5, context)
