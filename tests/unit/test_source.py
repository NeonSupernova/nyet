"""Unit tests for pynyet.source: byte-offset -> (line, col) tracking."""

from pynyet.source import SourceFile, Span


def test_line_col_start_of_file():
    sf = SourceFile("t.no", "abc\ndef")
    assert sf.line_col(0) == (1, 1)


def test_line_col_after_newline():
    sf = SourceFile("t.no", "abc\ndef")
    assert sf.line_col(4) == (2, 1)


def test_line_col_mid_line():
    sf = SourceFile("t.no", "abc\ndef")
    assert sf.line_col(5) == (2, 2)


def test_line_col_multiple_newlines():
    sf = SourceFile("t.no", "a\nb\nc\nd")
    assert sf.line_col(6) == (4, 1)


def test_line_text_returns_the_requested_line():
    sf = SourceFile("t.no", "first\nsecond\nthird")
    assert sf.line_text(2) == "second"


def test_line_text_out_of_range_returns_empty():
    sf = SourceFile("t.no", "only one line")
    assert sf.line_text(5) == ""


def test_span_text_extracts_the_slice():
    sf = SourceFile("t.no", "(let x 42)")
    span = Span(sf, 5, 6)
    assert span.text() == "x"


def test_span_merge_takes_the_union():
    sf = SourceFile("t.no", "(+ a b)")
    a = Span(sf, 3, 4)
    b = Span(sf, 5, 6)
    merged = a.merge(b)
    assert (merged.start, merged.end) == (3, 6)


def test_span_start_line_col_matches_source_file():
    sf = SourceFile("t.no", "abc\ndef")
    span = Span(sf, 4, 5)
    assert span.start_line_col() == (2, 1)
