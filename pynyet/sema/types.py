"""Internal type representations for the type checker.

These are *not* AST nodes — they are the semantic types used during checking.
AST TypeNode → NyetType happens during type resolution.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NyetType:
    """Base for all semantic types."""


@dataclass(frozen=True)
class IntType(NyetType):
    bits: int = 32
    signed: bool = True

    def __str__(self) -> str:
        prefix = "i" if self.signed else "u"
        return f"{prefix}{self.bits}"


@dataclass(frozen=True)
class FloatType(NyetType):
    bits: int = 64

    def __str__(self) -> str:
        return f"f{self.bits}"


@dataclass(frozen=True)
class BoolType(NyetType):
    def __str__(self) -> str:
        return "bool"


@dataclass(frozen=True)
class CharType(NyetType):
    def __str__(self) -> str:
        return "char"


@dataclass(frozen=True)
class StringType(NyetType):
    def __str__(self) -> str:
        return "string"


@dataclass(frozen=True)
class KeywordType(NyetType):
    """`:name` — an interned symbol, compared by identity, allocation-free."""

    def __str__(self) -> str:
        return "Keyword"


@dataclass(frozen=True)
class UnitType(NyetType):
    def __str__(self) -> str:
        return "unit"


@dataclass(frozen=True)
class FnSig(NyetType):
    params: tuple[NyetType, ...] = ()
    ret: NyetType = field(default_factory=UnitType)

    def __str__(self) -> str:
        ps = " ".join(str(p) for p in self.params)
        return f"(fn {ps} -> {self.ret})"


@dataclass(frozen=True)
class ArrayType(NyetType):
    element: NyetType = field(default_factory=lambda: IntType())

    def __str__(self) -> str:
        return f"Array[{self.element}]"


@dataclass(frozen=True)
class TupleType(NyetType):
    elements: tuple[NyetType, ...] = ()

    def __str__(self) -> str:
        return f"#({' '.join(str(e) for e in self.elements)})"


@dataclass(frozen=True)
class StructType(NyetType):
    name: str = ""
    fields: tuple[tuple[str, NyetType], ...] = ()

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class SumType(NyetType):
    name: str = ""
    variants: tuple[tuple[str, tuple[NyetType, ...]], ...] = ()

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class RefType(NyetType):
    inner: NyetType = field(default_factory=lambda: IntType())
    mutable: bool = False

    def __str__(self) -> str:
        return f"{'&!' if self.mutable else '&'}{self.inner}"


@dataclass(frozen=True)
class TypeVar(NyetType):
    """Unresolved type variable for inference."""

    id: int = 0

    def __str__(self) -> str:
        return f"?T{self.id}"


@dataclass(frozen=True)
class ErrorType(NyetType):
    """Placeholder for failed type resolution."""

    def __str__(self) -> str:
        return "<error>"


# Canonical singleton instances for common types
I8 = IntType(8, True)
I16 = IntType(16, True)
I32 = IntType(32, True)
I64 = IntType(64, True)
U8 = IntType(8, False)
U16 = IntType(16, False)
U32 = IntType(32, False)
U64 = IntType(64, False)
USIZE = IntType(64, False)  # platform-width, treat as u64
F32 = FloatType(32)
F64 = FloatType(64)
BOOL = BoolType()
CHAR = CharType()
STRING = StringType()
KEYWORD = KeywordType()
UNIT = UnitType()
ERROR = ErrorType()

# Map from type name strings to canonical types
PRIM_TYPES: dict[str, NyetType] = {
    "i8": I8,
    "i16": I16,
    "i32": I32,
    "i64": I64,
    "u8": U8,
    "u16": U16,
    "u32": U32,
    "u64": U64,
    "usize": USIZE,
    "f32": F32,
    "f64": F64,
    "bool": BOOL,
    "char": CHAR,
    "string": STRING,
    "Keyword": KEYWORD,
    "unit": UNIT,
}
