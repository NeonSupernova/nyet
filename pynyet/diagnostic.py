"""Diagnostics: structured error reporting with source spans."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .source import Span


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


@dataclass
class Diagnostic:
    severity: Severity
    message: str
    span: Optional[Span] = None
    hint: Optional[str] = None

    def format(self) -> str:
        parts: list[str] = []
        if self.span is not None:
            line, col = self.span.start_line_col()
            parts.append(f"{self.span.file.path}:{line}:{col}: {self.severity.value}: {self.message}")
            source_line = self.span.file.line_text(line)
            if source_line:
                parts.append(f"    {source_line}")
                caret_pad = " " * (col - 1)
                width = max(1, self.span.end - self.span.start)
                parts.append(f"    {caret_pad}{'^' * width}")
        else:
            parts.append(f"{self.severity.value}: {self.message}")
        if self.hint is not None:
            parts.append(f"  hint: {self.hint}")
        return "\n".join(parts)


class NyetError(Exception):
    """Raised when the compiler hits a fatal diagnostic."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.format())
        self.diagnostic = diagnostic


@dataclass
class DiagnosticSink:
    """Collects diagnostics from a compilation pass."""

    diagnostics: list[Diagnostic] = field(default_factory=list)

    def error(self, message: str, span: Optional[Span] = None, hint: Optional[str] = None) -> Diagnostic:
        d = Diagnostic(Severity.ERROR, message, span, hint)
        self.diagnostics.append(d)
        return d

    def warn(self, message: str, span: Optional[Span] = None, hint: Optional[str] = None) -> Diagnostic:
        d = Diagnostic(Severity.WARNING, message, span, hint)
        self.diagnostics.append(d)
        return d

    def has_errors(self) -> bool:
        return any(d.severity is Severity.ERROR for d in self.diagnostics)

    def format_all(self) -> str:
        return "\n\n".join(d.format() for d in self.diagnostics)
