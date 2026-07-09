"""Source file and span tracking for diagnostics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceFile:
    path: str
    text: str

    def line_col(self, offset: int) -> tuple[int, int]:
        """Return (line, column), both 1-indexed, for a byte offset."""
        line = 1
        col = 1
        for i, ch in enumerate(self.text):
            if i == offset:
                return line, col
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
        return line, col

    def line_text(self, line: int) -> str:
        lines = self.text.splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1]
        return ""


@dataclass(frozen=True)
class Span:
    """Half-open byte range [start, end) within a SourceFile."""

    file: SourceFile
    start: int
    end: int

    def merge(self, other: Span) -> Span:
        assert self.file is other.file, "cannot merge spans from different files"
        return Span(self.file, min(self.start, other.start), max(self.end, other.end))

    def text(self) -> str:
        return self.file.text[self.start : self.end]

    def start_line_col(self) -> tuple[int, int]:
        return self.file.line_col(self.start)

    def __repr__(self) -> str:
        line, col = self.start_line_col()
        return f"Span({self.file.path}:{line}:{col})"
