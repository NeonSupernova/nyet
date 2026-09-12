"""Unit tests for Emitter._struct_size_bytes / _sum_type_size_bytes.

Regression coverage for the struct heap-allocation alignment bug: sizing
malloc'd structs by naively summing field byte-sizes (then rounding the
*total* to 8) ignores interior padding, so a struct like
`{id:i32 name:string used:bool}` -- which LLVM actually lays out as 24
bytes (the i32 is padded to 8 to align the following pointer field) --
was only getting a 16-byte allocation. That under-allocation doesn't
crash on the first alloc; it corrupts the heap after enough repeated
allocations of the same struct shape, so a golden end-to-end build/run
test can't reliably catch it (whether the corruption surfaces as an
observable crash turns out to depend on incidental process/ASLR memory
layout -- verified empirically while writing this fix: the exact same
compiled binary crashed reliably when run as `./output` but never
crashed when copied into a tempdir first). Testing the size computation
directly is the only deterministic way to pin this down.

The same missing-interior-padding bug also existed in sum-type variant
payload storage (construction and pattern destructuring both computed
byte offsets via a naive running sum of _sizeof), so this file covers
_field_offsets / _sum_type_size_bytes too.
"""

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile


def _emitter_for(src: str) -> Emitter:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    e = Emitter()
    e.emit(program)
    return e


def test_struct_pads_i32_before_pointer_field():
    # {id:i32 name:string used:bool}: i32 (0..3), pad to 8 for the
    # pointer (8..15), then the bool (16), rounded up to 24.
    e = _emitter_for("""
        (struct Row id:i32 name:string used:bool)
        (fn main () -> unit unit)
    """)
    assert e._struct_size_bytes("Row") == 24


def test_struct_with_uniform_field_widths_needs_no_interior_padding():
    e = _emitter_for("""
        (struct ThreeInts a:i32 b:i32 c:i32)
        (fn main () -> unit unit)
    """)
    # 12 bytes of fields, no interior padding needed, rounded up to 16.
    assert e._struct_size_bytes("ThreeInts") == 16


def test_struct_of_bools_stays_small():
    e = _emitter_for("""
        (struct AllBool a:bool b:bool)
        (fn main () -> unit unit)
    """)
    assert e._struct_size_bytes("AllBool") == 8


def test_pointer_field_before_narrower_fields_needs_no_padding():
    # Pointer first means every later field is already aligned relative
    # to it -- this shape happens to sidestep the bug even under the old
    # naive sum, which is exactly why field order was a viable
    # workaround (see nyet-repo-state memory).
    e = _emitter_for("""
        (struct Row name:string id:i32 used:bool)
        (fn main () -> unit unit)
    """)
    assert e._struct_size_bytes("Row") == 16


def test_field_offsets_align_pointer_after_i32():
    e = _emitter_for("(fn main () -> unit unit)")
    assert e._field_offsets(["i32", "ptr"]) == [0, 8]


def test_field_offsets_no_padding_when_already_aligned():
    e = _emitter_for("(fn main () -> unit unit)")
    assert e._field_offsets(["i64", "double", "i64"]) == [0, 8, 16]


def test_sum_type_payload_accounts_for_interior_padding():
    e = _emitter_for("""
        (type Item (Entry i32 string) (Empty))
        (fn main () -> unit unit)
    """)
    # tag(4) + payload (i32 padded to 8, then the 8-byte ptr = 16) = 20,
    # rounded up to 24.
    assert e._sum_type_size_bytes("Item") == 24
