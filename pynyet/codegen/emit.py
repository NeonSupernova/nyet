"""LLVM IR text emitter for the Nyet compiler.

Walks the AST and emits LLVM IR as text strings (no llvmlite dependency).
The output can be compiled directly with `clang -o output output.ll`.

v0.2 scope: structs, field access, float arithmetic, type-aware calls,
sum types, match expressions — on top of v0.1 (fn, out, in, fmt, let,
var, if, do, arithmetic, comparisons).
"""

from __future__ import annotations

import struct as _struct

from pynyet.ast import nodes as N


class Emitter:
    """Emit LLVM IR text from a Nyet AST."""

    def __init__(self, target_triple: str = "") -> None:
        self._triple = target_triple
        self._lines: list[str] = []
        self._strings: dict[str, tuple[str, int, bytes]] = {}
        self._fmt_i32: str | None = None
        self._fmt_f64: str | None = None
        self._tmp = 0
        self._label = 0
        self._env: dict[str, tuple[str, str]] = {}  # name → (llvm_ptr, llvm_type)
        self._str_lits: dict[str, str] = {}
        self._declared_externs: set[str] = set()
        self._fn_lines: list[str] = []
        # Stack of (end_label, result_ptr | None, result_ty | None) for loop/break
        self._loop_stack: list[tuple[str, str | None, str | None]] = []
        # Alloca instructions hoisted to the function entry block
        self._fn_alloca_lines: list[str] = []

        # v0.2: struct registry — name → [(field_name, llvm_type)]
        self._structs: dict[str, list[tuple[str, str]]] = {}
        self._struct_type_lines: list[str] = []

        # v0.2: fn signature registry — name → (param_types, ret_type)
        self._fn_sigs: dict[str, tuple[list[str], str]] = {}

        # v0.2: sum type registry — name → [(variant_name, payload_types)]
        self._sum_types: dict[str, list[tuple[str, list[str]]]] = {}
        # variant_name → (sum_type_name, variant_index)
        self._variant_ctors: dict[str, tuple[str, int]] = {}

    # ==================================================================
    # Public entry point
    # ==================================================================

    def emit(self, program: list[N.Node]) -> str:
        fns: list[N.FnDecl] = []
        top_level: list[N.Node] = []
        has_main = False

        for node in program:
            if isinstance(node, N.StructDecl):
                self._register_struct(node)
            elif isinstance(node, N.TypeDecl):
                self._register_sum_type(node)
            elif isinstance(node, N.FnDecl):
                fns.append(node)
                if node.name == "main":
                    has_main = True
            else:
                top_level.append(node)

        for fn in fns:
            self._emit_fn(fn)

        if not has_main and top_level:
            self._emit_implicit_main(top_level)

        # Assemble module
        out: list[str] = []
        if self._triple:
            out.append(f'target triple = "{self._triple}"')
            out.append("")

        # Struct type definitions
        if self._struct_type_lines:
            out.extend(self._struct_type_lines)
            out.append("")

        # String constants
        for key, (name, byte_len, raw_bytes) in self._strings.items():
            escaped = self._escape_bytes(raw_bytes)
            out.append(
                f'{name} = private unnamed_addr constant '
                f'[{byte_len} x i8] c"{escaped}"'
            )
        if self._strings:
            out.append("")

        # Extern declarations
        for decl in sorted(self._declared_externs):
            out.append(decl)
        if self._declared_externs:
            out.append("")

        out.extend(self._lines)
        out.append("")
        return "\n".join(out)

    # ==================================================================
    # Struct / sum type registration
    # ==================================================================

    def _register_struct(self, node: N.StructDecl) -> None:
        fields = []
        for p in node.fields:
            ty = self._llvm_type(p.type)
            fields.append((p.name, ty))
        self._structs[node.name] = fields
        llvm_fields = ", ".join(ty for _, ty in fields)
        self._struct_type_lines.append(
            f"%{node.name} = type {{ {llvm_fields} }}"
        )

    def _register_sum_type(self, node: N.TypeDecl) -> None:
        """Register a sum type.  LLVM layout: { i32 tag, payload... }.

        Each variant is stored as { i32, <variant fields> }.  We pick the
        largest variant and pad smaller ones.  For simplicity in v0.2 we
        use an opaque byte array for the payload sized to the largest
        variant, plus the i32 tag.
        """
        variants: list[tuple[str, list[str]]] = []
        max_payload = 0
        for vname, vtypes in node.variants:
            field_types = [self._llvm_type(t) for t in vtypes]
            payload = sum(self._sizeof(t) for t in field_types)
            if payload > max_payload:
                max_payload = payload
            variants.append((vname, field_types))
            self._variant_ctors[vname] = (node.name, len(variants) - 1)

        self._sum_types[node.name] = variants

        if max_payload == 0:
            # Pure enum (all unit variants)
            self._struct_type_lines.append(
                f"%{node.name} = type {{ i32 }}"
            )
        else:
            self._struct_type_lines.append(
                f"%{node.name} = type {{ i32, [{max_payload} x i8] }}"
            )

    @staticmethod
    def _sizeof(ty: str) -> int:
        """Approximate size in bytes of an LLVM type (for payload sizing)."""
        if ty == "i1":
            return 1
        if ty == "i8":
            return 1
        if ty == "i16":
            return 2
        if ty in ("i32", "float"):
            return 4
        if ty in ("i64", "double", "ptr"):
            return 8
        if ty.startswith("%"):
            return 8  # struct pointer
        return 8

    # ==================================================================
    # Helpers
    # ==================================================================

    def _fresh_tmp(self) -> str:
        self._tmp += 1
        return f"%t{self._tmp}"

    def _fresh_label(self, prefix: str = "L") -> str:
        self._label += 1
        return f"{prefix}{self._label}"

    def _emit_line(self, line: str) -> None:
        self._fn_lines.append(f"  {line}")

    def _emit_label(self, name: str) -> None:
        self._fn_lines.append(f"{name}:")

    def _emit_alloca(self, ty: str) -> str:
        """Allocate a stack slot hoisted to the function entry block."""
        ptr = self._fresh_tmp()
        self._fn_alloca_lines.append(f"  {ptr} = alloca {ty}")
        return ptr

    def _get_string(self, value: str) -> tuple[str, int]:
        key = f"str:{value}"
        if key in self._strings:
            return self._strings[key][0], self._strings[key][1]
        idx = len(self._strings)
        name = f"@.str.{idx}"
        raw = value.encode("utf-8") + b"\x00"
        self._strings[key] = (name, len(raw), raw)
        return name, len(raw)

    def _get_format_string(self, fmt: str, key_suffix: str) -> str:
        key = f"fmt:{key_suffix}"
        if key in self._strings:
            return self._strings[key][0]
        idx = len(self._strings)
        name = f"@.str.{idx}"
        raw = fmt.encode("utf-8") + b"\x00"
        self._strings[key] = (name, len(raw), raw)
        return name

    def _declare_printf(self) -> None:
        self._declared_externs.add("declare i32 @printf(ptr, ...)")

    def _declare_extern(self, decl: str) -> None:
        self._declared_externs.add(decl)

    def _get_fmt_str(self) -> str:
        return self._get_format_string("%s", "str")

    def _get_fmt_i32(self) -> str:
        if self._fmt_i32 is None:
            self._fmt_i32 = self._get_format_string("%d", "i32")
        return self._fmt_i32

    def _get_fmt_f64(self) -> str:
        if self._fmt_f64 is None:
            self._fmt_f64 = self._get_format_string("%g", "f64")
        return self._fmt_f64

    @staticmethod
    def _escape_bytes(data: bytes) -> str:
        result = []
        for b in data:
            if 32 <= b < 127 and b not in (ord('"'), ord('\\')):
                result.append(chr(b))
            else:
                result.append(f"\\{b:02X}")
        return "".join(result)

    # ==================================================================
    # Type mapping
    # ==================================================================

    def _llvm_type(self, tn: N.TypeNode | None) -> str:
        if tn is None:
            return "i32"
        if isinstance(tn, (N.PrimType, N.NamedType)):
            name = tn.name
            if name in ("i32", "int"):
                return "i32"
            if name == "i64":
                return "i64"
            if name in ("f64", "double"):
                return "double"
            if name in ("f32", "float"):
                return "float"
            if name == "bool":
                return "i1"
            if name == "string":
                return "ptr"
            if name in ("i8", "u8"):
                return "i8"
            if name in ("i16", "u16"):
                return "i16"
            # Struct or sum type — pass by pointer
            if name in self._structs or name in self._sum_types:
                return "ptr"
        if isinstance(tn, N.UnitType):
            return "void"
        return "i32"

    def _llvm_ret_type(self, tn: N.TypeNode | None) -> str:
        if tn is None:
            return "void"
        return self._llvm_type(tn)

    def _nyet_type_name(self, tn: N.TypeNode | None) -> str | None:
        """Return the Nyet type name if it's a named/prim type."""
        if isinstance(tn, (N.PrimType, N.NamedType)):
            return tn.name
        return None

    @staticmethod
    def _llvm_type_from_name(name: str) -> str:
        mapping = {
            "i32": "i32", "int": "i32", "i64": "i64",
            "f64": "double", "f32": "float",
            "bool": "i1", "string": "ptr",
        }
        return mapping.get(name, "i32")

    def _is_float(self, ty: str) -> bool:
        return ty in ("double", "float")

    # ==================================================================
    # Top-level emission
    # ==================================================================

    def _emit_fn(self, node: N.FnDecl) -> None:
        self._tmp = 0
        self._label = 0
        self._fn_lines = []
        self._fn_alloca_lines = []
        saved_env = dict(self._env)

        if node.name == "main":
            self._lines.append("define i32 @main() {")
            self._emit_label("entry")
            if node.body is not None:
                self._emit_expr(node.body)
            self._emit_line("ret i32 0")
        else:
            param_types = []
            param_names = []
            param_nyet_names = []  # original Nyet type names for struct detection
            for p in node.params:
                param_types.append(self._llvm_type(p.type))
                param_names.append(p.name)
                param_nyet_names.append(self._nyet_type_name(p.type))

            ret_type = self._llvm_ret_type(node.return_type)
            self._fn_sigs[node.name] = (param_types, ret_type)

            params_str = ", ".join(
                f"{t} %{n}" for t, n in zip(param_types, param_names)
            )
            self._lines.append(
                f"define {ret_type} @{node.name}({params_str}) {{"
            )
            self._emit_label("entry")

            for t, n, nyet_n in zip(param_types, param_names, param_nyet_names):
                if nyet_n and (nyet_n in self._structs or nyet_n in self._sum_types):
                    # Struct/sum params are already ptrs — register directly
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    self._env_struct_name[n] = nyet_n
                else:
                    ptr = self._emit_alloca(t)
                    self._emit_line(f"store {t} %{n}, ptr {ptr}")
                    self._env[n] = (ptr, t)

            if node.body is not None:
                result = self._emit_expr(node.body)
                if ret_type == "void":
                    self._emit_line("ret void")
                elif result is not None:
                    self._emit_line(f"ret {ret_type} {result}")
                else:
                    self._emit_line(f"ret {ret_type} 0")
            else:
                self._emit_line("ret void" if ret_type == "void" else f"ret {ret_type} 0")

        self._splice_fn_lines()
        self._lines.append("}")
        self._lines.append("")
        self._env = saved_env

    def _emit_implicit_main(self, stmts: list[N.Node]) -> None:
        self._tmp = 0
        self._label = 0
        self._fn_lines = []
        self._fn_alloca_lines = []
        saved_env = dict(self._env)
        self._lines.append("define i32 @main() {")
        self._emit_label("entry")
        for stmt in stmts:
            self._emit_expr(stmt)
        self._emit_line("ret i32 0")
        self._splice_fn_lines()
        self._lines.append("}")
        self._lines.append("")
        self._env = saved_env

    def _splice_fn_lines(self) -> None:
        """Append fn body to _lines, hoisting alloca instructions after entry:."""
        if not self._fn_lines:
            return
        # fn_lines[0] is always "entry:" — put hoisted allocas right after it
        self._lines.append(self._fn_lines[0])  # "entry:"
        self._lines.extend(self._fn_alloca_lines)
        self._lines.extend(self._fn_lines[1:])

    # ==================================================================
    # Type inference
    # ==================================================================

    def _infer_llvm_type(self, node: N.Node) -> str:
        if isinstance(node, N.IntLit):
            return "i32"
        if isinstance(node, N.FloatLit):
            return "double"
        if isinstance(node, N.BoolLit):
            return "i1"
        if isinstance(node, N.StringLit):
            return "ptr"
        if isinstance(node, N.Ident) and node.name in self._env:
            return self._env[node.name][1]
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            op = node.head.name
            if op in ("+", "-", "*", "/", "%"):
                has_double = False
                has_float = False
                for a in node.args:
                    t = self._infer_llvm_type(a)
                    if t == "double":
                        has_double = True
                    elif t == "float":
                        has_float = True
                if has_double:
                    return "double"
                if has_float:
                    return "float"
                return "i32"
            if op in ("==", "!=", "<", ">", "<=", ">=", "&&", "||", "!"):
                return "i1"
            if op == "fmt":
                return "ptr"
            if op == "in":
                if node.args and isinstance(node.args[0], N.Ident):
                    return self._llvm_type_from_name(node.args[0].name)
                return "ptr"
            if op in self._structs or op in self._variant_ctors:
                return "ptr"
            if op in self._fn_sigs:
                return self._fn_sigs[op][1]
        if isinstance(node, N.FieldAccess):
            return self._infer_field_type(node)
        if isinstance(node, N.If) and node.then_branch:
            return self._infer_llvm_type(node.then_branch)
        if isinstance(node, N.Do) and node.exprs:
            return self._infer_llvm_type(node.exprs[-1])
        return "i32"

    def _infer_field_type(self, node: N.FieldAccess) -> str:
        """Infer the LLVM type of a field access."""
        # Figure out which struct type the target is
        target = node.target
        struct_name = self._struct_name_of(target)
        if struct_name and struct_name in self._structs:
            for fname, ftype in self._structs[struct_name]:
                if fname == node.field_name:
                    return ftype
        return "i32"

    def _struct_name_of(self, node: N.Node) -> str | None:
        """Try to determine which Nyet struct a node refers to."""
        if isinstance(node, N.Ident) and node.name in self._env:
            # Check if we've recorded the struct name for this binding
            return self._env_struct_name.get(node.name)
        return None

    # ==================================================================
    # Expression emission
    # ==================================================================

    def _emit_expr(self, node: N.Node) -> str | None:
        if isinstance(node, N.IntLit):
            return str(node.value)

        if isinstance(node, N.FloatLit):
            packed = _struct.pack("d", node.value)
            as_int = _struct.unpack("Q", packed)[0]
            return f"0x{as_int:016X}"

        if isinstance(node, N.BoolLit):
            return "1" if node.value else "0"

        if isinstance(node, N.StringLit):
            name, _ = self._get_string(node.value)
            return name

        if isinstance(node, N.UnitLit):
            return None

        if isinstance(node, N.Pass):
            return None

        if isinstance(node, N.Ident):
            return self._emit_ident(node)

        if isinstance(node, N.Call):
            return self._emit_call(node)

        if isinstance(node, N.FieldAccess):
            return self._emit_field_access(node)

        if isinstance(node, N.If):
            return self._emit_if(node)

        if isinstance(node, N.Match):
            return self._emit_match(node)

        if isinstance(node, N.Do):
            return self._emit_do(node)

        if isinstance(node, N.LetDecl):
            return self._emit_let(node)

        if isinstance(node, N.Return):
            if node.value is not None:
                val = self._emit_expr(node.value)
                ty = self._infer_llvm_type(node.value)
                self._emit_line(f"ret {ty} {val}")
            else:
                self._emit_line("ret void")
            return None

        if isinstance(node, N.Assign):
            return self._emit_assign(node)

        if isinstance(node, N.Loop):
            return self._emit_loop(node)

        if isinstance(node, N.Break):
            return self._emit_break(node)

        if isinstance(node, (N.StructDecl, N.TypeDecl)):
            return None  # already processed in emit()

        return None

    def _emit_ident(self, node: N.Ident) -> str | None:
        if node.name in self._env:
            ptr, ty = self._env[node.name]
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = load {ty}, ptr {ptr}")
            return tmp
        return None

    # ==================================================================
    # Calls — builtins, operators, struct ctors, user fns
    # ==================================================================

    def _emit_call(self, node: N.Call) -> str | None:
        if isinstance(node.head, N.Ident):
            name = node.head.name
            # Builtins
            if name == "out":
                return self._emit_out(node.args)
            if name == "in":
                return self._emit_in(node.args)
            if name == "fmt":
                return self._emit_fmt(node.args)
            # Operators
            if name in ("+", "-", "*", "/", "%"):
                return self._emit_arith(name, node.args)
            if name in ("==", "!=", "<", ">", "<=", ">="):
                return self._emit_cmp(name, node.args)
            if name in ("&&", "||", "!"):
                return self._emit_bool_op(name, node.args)
            # Struct constructor
            if name in self._structs:
                return self._emit_struct_construct(name, node.args)
            # Sum type variant constructor
            if name in self._variant_ctors:
                return self._emit_variant_construct(name, node.args)
            # User function
            return self._emit_user_call(name, node.args)
        return None

    # ------------------------------------------------------------------
    # out
    # ------------------------------------------------------------------

    def _emit_out(self, args: list[N.Expr]) -> str | None:
        for arg in args:
            val = self._emit_expr(arg)
            if val is None:
                continue
            ty = self._infer_llvm_type(arg)
            self._declare_printf()
            if ty == "ptr":
                fmt = self._get_fmt_str()
                tmp = self._fresh_tmp()
                self._emit_line(
                    f"{tmp} = call i32 (ptr, ...) @printf(ptr {fmt}, ptr {val})"
                )
            elif self._is_float(ty):
                fmt = self._get_fmt_f64()
                tmp = self._fresh_tmp()
                if ty == "float":
                    ext = self._fresh_tmp()
                    self._emit_line(f"{ext} = fpext float {val} to double")
                    val = ext
                self._emit_line(
                    f"{tmp} = call i32 (ptr, ...) @printf(ptr {fmt}, double {val})"
                )
            else:
                fmt = self._get_fmt_i32()
                tmp = self._fresh_tmp()
                self._emit_line(
                    f"{tmp} = call i32 (ptr, ...) @printf(ptr {fmt}, i32 {val})"
                )
        return None

    # ------------------------------------------------------------------
    # in
    # ------------------------------------------------------------------

    def _emit_in(self, args: list[N.Expr]) -> str | None:
        self._declare_extern("declare ptr @fgets(ptr, i32, ptr)")
        self._declare_extern("declare ptr @fdopen(i32, ptr)")
        self._declare_printf()

        buf = self._emit_alloca("[256 x i8]")
        buf_ptr = self._fresh_tmp()
        self._emit_line(
            f"{buf_ptr} = getelementptr [256 x i8], ptr {buf}, i32 0, i32 0"
        )
        mode_name = self._get_format_string("r", "r_mode")
        stdin_fp = self._fresh_tmp()
        self._emit_line(
            f"{stdin_fp} = call ptr @fdopen(i32 0, ptr {mode_name})"
        )
        tmp = self._fresh_tmp()
        self._emit_line(
            f"{tmp} = call ptr @fgets(ptr {buf_ptr}, i32 256, ptr {stdin_fp})"
        )

        target_type = "i32"
        if args and isinstance(args[0], N.Ident):
            target_type = self._llvm_type_from_name(args[0].name)

        if target_type in ("i32", "i64"):
            return self._emit_in_int(buf_ptr, target_type)
        return buf_ptr

    def _emit_in_int(self, buf_ptr: str, target_type: str) -> str:
        self._declare_extern("declare i64 @strtol(ptr, ptr, i32)")
        self._declare_extern("declare void @exit(i32)")

        endptr = self._emit_alloca("ptr")
        val64 = self._fresh_tmp()
        self._emit_line(
            f"{val64} = call i64 @strtol(ptr {buf_ptr}, ptr {endptr}, i32 10)"
        )
        end = self._fresh_tmp()
        self._emit_line(f"{end} = load ptr, ptr {endptr}")
        end_char = self._fresh_tmp()
        self._emit_line(f"{end_char} = load i8, ptr {end}")

        moved = self._fresh_tmp()
        self._emit_line(f"{moved} = icmp ne ptr {end}, {buf_ptr}")

        checks = []
        for ch_val in (10, 13, 32, 0):
            c = self._fresh_tmp()
            self._emit_line(f"{c} = icmp eq i8 {end_char}, {ch_val}")
            checks.append(c)
        ws = checks[0]
        for c in checks[1:]:
            nxt = self._fresh_tmp()
            self._emit_line(f"{nxt} = or i1 {ws}, {c}")
            ws = nxt

        valid = self._fresh_tmp()
        self._emit_line(f"{valid} = and i1 {moved}, {ws}")

        ok_label = self._fresh_label("in_ok")
        err_label = self._fresh_label("in_err")
        self._emit_line(
            f"br i1 {valid}, label %{ok_label}, label %{err_label}"
        )

        self._emit_label(err_label)
        err_msg = self._get_format_string(
            "error: expected integer, got invalid input\n", "in_err_i32"
        )
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call i32 (ptr, ...) @printf(ptr {err_msg})")
        self._emit_line("call void @exit(i32 1)")
        self._emit_line("unreachable")

        self._emit_label(ok_label)
        if target_type == "i32":
            result = self._fresh_tmp()
            self._emit_line(f"{result} = trunc i64 {val64} to i32")
            return result
        return val64

    # ------------------------------------------------------------------
    # fmt
    # ------------------------------------------------------------------

    def _emit_fmt(self, args: list[N.Expr]) -> str | None:
        if not args:
            return None
        self._declare_extern("declare i32 @snprintf(ptr, i32, ptr, ...)")

        template_val = self._emit_expr(args[0])
        if template_val is None:
            return None

        fmt_args: list[tuple[str, str]] = []
        for arg in args[1:]:
            val = self._emit_expr(arg)
            if val is not None:
                ty = self._infer_llvm_type(arg)
                fmt_args.append((ty, val))

        template_text = None
        if isinstance(args[0], N.StringLit):
            template_text = args[0].value
        elif isinstance(args[0], N.Ident) and args[0].name in self._str_lits:
            template_text = self._str_lits[args[0].name]

        if template_text is not None:
            c_fmt = template_text
            for llvm_ty, _ in fmt_args:
                spec = "%s" if llvm_ty == "ptr" else "%g" if self._is_float(llvm_ty) else "%d"
                c_fmt = c_fmt.replace("{}", spec, 1)
            fmt_name = self._get_format_string(c_fmt, f"fmt_{id(args[0])}")
        else:
            fmt_name = template_val

        buf = self._emit_alloca("[1024 x i8]")
        buf_ptr = self._fresh_tmp()
        self._emit_line(
            f"{buf_ptr} = getelementptr [1024 x i8], ptr {buf}, i32 0, i32 0"
        )
        snprintf_args = f"ptr {buf_ptr}, i32 1024, ptr {fmt_name}"
        for llvm_ty, val in fmt_args:
            snprintf_args += f", {llvm_ty} {val}"
        tmp = self._fresh_tmp()
        self._emit_line(
            f"{tmp} = call i32 (ptr, i32, ptr, ...) @snprintf({snprintf_args})"
        )
        return buf_ptr

    # ------------------------------------------------------------------
    # Arithmetic (type-aware: int or float)
    # ------------------------------------------------------------------

    def _emit_arith(self, op: str, args: list[N.Expr]) -> str | None:
        if len(args) < 2:
            return None
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            return None

        lty = self._infer_llvm_type(args[0])
        rty = self._infer_llvm_type(args[1])

        if self._is_float(lty) or self._is_float(rty):
            # Use double if either operand is double, else float
            fty = "double" if "double" in (lty, rty) else "float"
            if not self._is_float(lty):
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = sitofp i32 {lhs} to {fty}")
                lhs = conv
            elif lty != fty:
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = fpext float {lhs} to double")
                lhs = conv
            if not self._is_float(rty):
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = sitofp i32 {rhs} to {fty}")
                rhs = conv
            elif rty != fty:
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = fpext float {rhs} to double")
                rhs = conv
            tmp = self._fresh_tmp()
            fops = {"+": "fadd", "-": "fsub", "*": "fmul", "/": "fdiv",
                    "%": "frem"}
            self._emit_line(f"{tmp} = {fops[op]} {fty} {lhs}, {rhs}")
            return tmp
        else:
            tmp = self._fresh_tmp()
            iops = {"+": "add", "-": "sub", "*": "mul", "/": "sdiv",
                    "%": "srem"}
            self._emit_line(f"{tmp} = {iops[op]} i32 {lhs}, {rhs}")
            return tmp

    # ------------------------------------------------------------------
    # Comparisons (type-aware)
    # ------------------------------------------------------------------

    def _emit_cmp(self, op: str, args: list[N.Expr]) -> str | None:
        if len(args) < 2:
            return None
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            return None

        lty = self._infer_llvm_type(args[0])
        rty = self._infer_llvm_type(args[1])

        if self._is_float(lty) or self._is_float(rty):
            fty = "double" if "double" in (lty, rty) else "float"
            if not self._is_float(lty):
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = sitofp i32 {lhs} to {fty}")
                lhs = conv
            elif lty != fty:
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = fpext float {lhs} to double")
                lhs = conv
            if not self._is_float(rty):
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = sitofp i32 {rhs} to {fty}")
                rhs = conv
            elif rty != fty:
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = fpext float {rhs} to double")
                rhs = conv
            tmp = self._fresh_tmp()
            fconds = {"==": "oeq", "!=": "one", "<": "olt", ">": "ogt",
                      "<=": "ole", ">=": "oge"}
            self._emit_line(f"{tmp} = fcmp {fconds[op]} {fty} {lhs}, {rhs}")
            return tmp
        else:
            tmp = self._fresh_tmp()
            iconds = {"==": "eq", "!=": "ne", "<": "slt", ">": "sgt",
                      "<=": "sle", ">=": "sge"}
            self._emit_line(f"{tmp} = icmp {iconds[op]} i32 {lhs}, {rhs}")
            return tmp

    # ------------------------------------------------------------------
    # Boolean operators
    # ------------------------------------------------------------------

    def _emit_bool_op(self, op: str, args: list[N.Expr]) -> str | None:
        if op == "!" and args:
            val = self._emit_expr(args[0])
            if val is None:
                return None
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = xor i1 {val}, 1")
            return tmp
        if len(args) < 2:
            return None
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            return None
        tmp = self._fresh_tmp()
        self._emit_line(
            f"{tmp} = {'and' if op == '&&' else 'or'} i1 {lhs}, {rhs}"
        )
        return tmp

    # ------------------------------------------------------------------
    # Struct construction
    # ------------------------------------------------------------------

    def _emit_struct_construct(self, name: str, args: list[N.Expr]) -> str:
        """Emit `(StructName field:val ...)` → alloca + store fields."""
        fields = self._structs[name]
        ptr = self._emit_alloca(f"%{name}")

        # Match args to fields — support both positional and keyword
        vals: dict[str, str] = {}
        positional = 0
        for arg in args:
            if isinstance(arg, N.KeywordArg) and arg.value is not None:
                v = self._emit_expr(arg.value)
                if v is not None:
                    vals[arg.name] = v
            else:
                v = self._emit_expr(arg)
                if v is not None and positional < len(fields):
                    vals[fields[positional][0]] = v
                positional += 1

        for i, (fname, ftype) in enumerate(fields):
            if fname in vals:
                fptr = self._fresh_tmp()
                self._emit_line(
                    f"{fptr} = getelementptr inbounds %{name}, ptr {ptr}, "
                    f"i32 0, i32 {i}"
                )
                self._emit_line(f"store {ftype} {vals[fname]}, ptr {fptr}")

        return ptr

    # ------------------------------------------------------------------
    # Sum type variant construction
    # ------------------------------------------------------------------

    def _emit_variant_construct(self, vname: str, args: list[N.Expr]) -> str:
        """Emit `(VariantName payload...)` → alloca sum type + store tag + payload."""
        sum_name, tag_idx = self._variant_ctors[vname]
        variants = self._sum_types[sum_name]
        _, payload_types = variants[tag_idx]

        ptr = self._emit_alloca(f"%{sum_name}")

        # Store tag
        tag_ptr = self._fresh_tmp()
        self._emit_line(
            f"{tag_ptr} = getelementptr inbounds %{sum_name}, ptr {ptr}, "
            f"i32 0, i32 0"
        )
        self._emit_line(f"store i32 {tag_idx}, ptr {tag_ptr}")

        # Store payload fields
        if payload_types:
            payload_ptr = self._fresh_tmp()
            self._emit_line(
                f"{payload_ptr} = getelementptr inbounds %{sum_name}, ptr {ptr}, "
                f"i32 0, i32 1"
            )
            offset = 0
            for i, arg in enumerate(args):
                if i >= len(payload_types):
                    break
                val = self._emit_expr(arg)
                if val is not None:
                    ftype = payload_types[i]
                    if offset == 0:
                        field_ptr = payload_ptr
                    else:
                        field_ptr = self._fresh_tmp()
                        self._emit_line(
                            f"{field_ptr} = getelementptr i8, ptr {payload_ptr}, "
                            f"i32 {offset}"
                        )
                    self._emit_line(f"store {ftype} {val}, ptr {field_ptr}")
                    offset += self._sizeof(ftype)

        return ptr

    # ------------------------------------------------------------------
    # Field access
    # ------------------------------------------------------------------

    def _emit_field_access(self, node: N.FieldAccess) -> str | None:
        """Emit `.field target` → GEP + load."""
        target_val = self._emit_expr(node.target)
        if target_val is None:
            return None

        struct_name = self._struct_name_of(node.target)
        if struct_name is None or struct_name not in self._structs:
            return None

        fields = self._structs[struct_name]
        for i, (fname, ftype) in enumerate(fields):
            if fname == node.field_name:
                fptr = self._fresh_tmp()
                self._emit_line(
                    f"{fptr} = getelementptr inbounds %{struct_name}, "
                    f"ptr {target_val}, i32 0, i32 {i}"
                )
                result = self._fresh_tmp()
                self._emit_line(f"{result} = load {ftype}, ptr {fptr}")
                return result
        return None

    # ------------------------------------------------------------------
    # Match expression
    # ------------------------------------------------------------------

    def _emit_match(self, node: N.Match) -> str | None:
        """Emit a match expression (v0.2: variant tag matching)."""
        scrut = self._emit_expr(node.scrutinee)
        if scrut is None:
            return None

        # Determine the sum type of the scrutinee
        sum_name = None
        if isinstance(node.scrutinee, N.Ident):
            sum_name = self._env_struct_name.get(node.scrutinee.name)

        if sum_name is None or sum_name not in self._sum_types:
            # Can't determine sum type — try simple value matching
            return self._emit_match_simple(scrut, node)

        # Load the tag
        tag_ptr = self._fresh_tmp()
        self._emit_line(
            f"{tag_ptr} = getelementptr inbounds %{sum_name}, "
            f"ptr {scrut}, i32 0, i32 0"
        )
        tag = self._fresh_tmp()
        self._emit_line(f"{tag} = load i32, ptr {tag_ptr}")

        end_label = self._fresh_label("match_end")
        result_ty = self._infer_llvm_type(node.arms[0].body) if node.arms else "i32"
        result_ptr = self._emit_alloca(result_ty)

        variants = self._sum_types[sum_name]
        default_label = self._fresh_label("match_default")

        # Build arms
        arm_labels = []
        for arm in node.arms:
            arm_labels.append(self._fresh_label("match_arm"))

        # Switch on tag
        cases = []
        for arm, arm_label in zip(node.arms, arm_labels):
            pat = arm.pattern
            if isinstance(pat, N.VariantPat) and pat.name in self._variant_ctors:
                _, tag_idx = self._variant_ctors[pat.name]
                cases.append(f"i32 {tag_idx}, label %{arm_label}")
            elif isinstance(pat, N.WildPat) or (isinstance(pat, N.VarPat)):
                # Wildcard/variable — becomes default
                cases.append(None)  # mark as default
            else:
                cases.append(None)

        case_strs = [c for c in cases if c is not None]
        switch_cases = "\n    ".join(f"{c}" for c in case_strs)
        self._emit_line(
            f"switch i32 {tag}, label %{default_label} [\n    {switch_cases}\n  ]"
        )

        # Emit each arm
        for arm, arm_label, case in zip(node.arms, arm_labels, cases):
            if case is None:
                # This is the default arm
                self._emit_label(default_label)
                default_label = None  # mark as used
            else:
                self._emit_label(arm_label)

            saved = dict(self._env)

            # Bind pattern variables
            pat = arm.pattern
            if isinstance(pat, N.VariantPat) and pat.name in self._variant_ctors:
                _, tag_idx = self._variant_ctors[pat.name]
                payload_types = variants[tag_idx][1]

                if pat.args and payload_types:
                    payload_ptr = self._fresh_tmp()
                    self._emit_line(
                        f"{payload_ptr} = getelementptr inbounds "
                        f"%{sum_name}, ptr {scrut}, i32 0, i32 1"
                    )
                    offset = 0
                    for pi, ppat in enumerate(pat.args):
                        if pi >= len(payload_types):
                            break
                        pty = payload_types[pi]
                        if offset == 0:
                            fld_ptr = payload_ptr
                        else:
                            fld_ptr = self._fresh_tmp()
                            self._emit_line(
                                f"{fld_ptr} = getelementptr i8, "
                                f"ptr {payload_ptr}, i32 {offset}"
                            )
                        if isinstance(ppat, N.VarPat):
                            val = self._fresh_tmp()
                            self._emit_line(
                                f"{val} = load {pty}, ptr {fld_ptr}"
                            )
                            vptr = self._emit_alloca(pty)
                            self._emit_line(
                                f"store {pty} {val}, ptr {vptr}"
                            )
                            self._env[ppat.name] = (vptr, pty)
                        offset += self._sizeof(pty)

            elif isinstance(pat, N.VarPat):
                # Bind whole scrutinee
                vptr = self._emit_alloca("ptr")
                self._emit_line(f"store ptr {scrut}, ptr {vptr}")
                self._env[pat.name] = (vptr, "ptr")

            body_val = self._emit_expr(arm.body)
            if body_val is not None:
                self._emit_line(f"store {result_ty} {body_val}, ptr {result_ptr}")
            self._emit_line(f"br label %{end_label}")
            self._env = saved

        # If no default was used, emit an empty one
        if default_label is not None:
            self._emit_label(default_label)
            self._emit_line(f"br label %{end_label}")

        self._emit_label(end_label)
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {result_ty}, ptr {result_ptr}")
        return result

    def _emit_match_simple(self, scrut: str, node: N.Match) -> str | None:
        """Simple value-based match (integers, etc.)."""
        end_label = self._fresh_label("match_end")
        result_ty = self._infer_llvm_type(node.arms[0].body) if node.arms else "i32"
        result_ptr = self._emit_alloca(result_ty)

        next_label = self._fresh_label("match_next")
        for i, arm in enumerate(node.arms):
            is_last = i == len(node.arms) - 1
            pat = arm.pattern

            if isinstance(pat, N.WildPat) or isinstance(pat, N.VarPat):
                # Default arm
                if isinstance(pat, N.VarPat):
                    saved = dict(self._env)
                    vptr = self._emit_alloca("i32")
                    self._emit_line(f"store i32 {scrut}, ptr {vptr}")
                    self._env[pat.name] = (vptr, "i32")

                body_val = self._emit_expr(arm.body)
                if body_val is not None:
                    self._emit_line(
                        f"store {result_ty} {body_val}, ptr {result_ptr}"
                    )
                self._emit_line(f"br label %{end_label}")

                if isinstance(pat, N.VarPat):
                    self._env = saved
                break

            elif isinstance(pat, N.LitPat):
                cmp_val = self._emit_expr(pat.value)
                cmp = self._fresh_tmp()
                self._emit_line(f"{cmp} = icmp eq i32 {scrut}, {cmp_val}")

                arm_label = self._fresh_label("match_arm")
                if is_last:
                    next_label = self._fresh_label("match_default")
                else:
                    next_label = self._fresh_label("match_next")

                self._emit_line(
                    f"br i1 {cmp}, label %{arm_label}, label %{next_label}"
                )

                self._emit_label(arm_label)
                body_val = self._emit_expr(arm.body)
                if body_val is not None:
                    self._emit_line(
                        f"store {result_ty} {body_val}, ptr {result_ptr}"
                    )
                self._emit_line(f"br label %{end_label}")

                self._emit_label(next_label)

        self._emit_line(f"br label %{end_label}")
        self._emit_label(end_label)
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {result_ty}, ptr {result_ptr}")
        return result

    # ------------------------------------------------------------------
    # If expression
    # ------------------------------------------------------------------

    def _emit_if(self, node: N.If) -> str | None:
        cond = self._emit_expr(node.cond)
        if cond is None:
            return None

        then_label = self._fresh_label("then")
        else_label = self._fresh_label("else")
        end_label = self._fresh_label("ifend")

        result_ty = self._infer_llvm_type(node.then_branch) if node.then_branch else "ptr"
        result_ptr = self._emit_alloca(result_ty)
        # Zero-initialize
        if result_ty == "ptr":
            self._emit_line(f"store ptr null, ptr {result_ptr}")
        elif self._is_float(result_ty):
            self._emit_line(f"store {result_ty} 0.0, ptr {result_ptr}")
        else:
            self._emit_line(f"store {result_ty} 0, ptr {result_ptr}")

        self._emit_line(
            f"br i1 {cond}, label %{then_label}, label %{else_label}"
        )

        self._emit_label(then_label)
        then_val = self._emit_expr(node.then_branch)
        if then_val is not None:
            self._emit_line(f"store {result_ty} {then_val}, ptr {result_ptr}")
        self._emit_line(f"br label %{end_label}")

        self._emit_label(else_label)
        if node.else_branch is not None:
            else_val = self._emit_expr(node.else_branch)
            if else_val is not None:
                self._emit_line(
                    f"store {result_ty} {else_val}, ptr {result_ptr}"
                )
        self._emit_line(f"br label %{end_label}")

        self._emit_label(end_label)
        if then_val is not None:
            result = self._fresh_tmp()
            self._emit_line(
                f"{result} = load {result_ty}, ptr {result_ptr}"
            )
            return result
        return None

    # ------------------------------------------------------------------
    # Do / Let / Assign
    # ------------------------------------------------------------------

    def _emit_do(self, node: N.Do) -> str | None:
        result = None
        for expr in node.exprs:
            result = self._emit_expr(expr)
        return result

    # ------------------------------------------------------------------
    # Loop / Break
    # ------------------------------------------------------------------

    def _find_break_type(self, node: N.Node | None) -> str | None:
        """Walk the subtree to find the LLVM type carried by the first break."""
        if node is None:
            return None
        if isinstance(node, N.Break):
            return self._infer_llvm_type(node.value) if node.value is not None else None
        if isinstance(node, N.Loop):
            return None  # nested loop owns its own breaks
        if isinstance(node, N.Do):
            for e in node.exprs:
                t = self._find_break_type(e)
                if t is not None:
                    return t
        if isinstance(node, N.If):
            t = self._find_break_type(node.then_branch)
            if t is not None:
                return t
            return self._find_break_type(node.else_branch)
        return None

    def _emit_loop(self, node: N.Loop) -> str | None:
        top_label = self._fresh_label("loop_top")
        end_label = self._fresh_label("loop_end")

        # Allocate result slot for break-with-value
        break_ty = self._find_break_type(node.body)
        result_ptr: str | None = None
        if break_ty is not None:
            result_ptr = self._emit_alloca(break_ty)
            if break_ty == "ptr":
                self._emit_line(f"store ptr null, ptr {result_ptr}")
            elif self._is_float(break_ty):
                self._emit_line(f"store {break_ty} 0.0, ptr {result_ptr}")
            else:
                self._emit_line(f"store {break_ty} 0, ptr {result_ptr}")

        self._emit_line(f"br label %{top_label}")
        self._emit_label(top_label)

        self._loop_stack.append((end_label, result_ptr, break_ty))
        if node.body is not None:
            self._emit_expr(node.body)
        self._loop_stack.pop()

        # Fall-through back to loop top (unreachable if body always breaks)
        self._emit_line(f"br label %{top_label}")
        self._emit_label(end_label)

        if result_ptr is not None and break_ty is not None:
            result = self._fresh_tmp()
            self._emit_line(f"{result} = load {break_ty}, ptr {result_ptr}")
            return result
        return None

    def _emit_break(self, node: N.Break) -> str | None:
        if self._loop_stack:
            end_label, result_ptr, break_ty = self._loop_stack[-1]
            if node.value is not None and result_ptr is not None and break_ty is not None:
                val = self._emit_expr(node.value)
                if val is not None:
                    val_ty = self._infer_llvm_type(node.value)
                    if self._is_float(break_ty) and not self._is_float(val_ty):
                        conv = self._fresh_tmp()
                        self._emit_line(f"{conv} = sitofp {val_ty} {val} to {break_ty}")
                        val = conv
                    self._emit_line(f"store {break_ty} {val}, ptr {result_ptr}")
            self._emit_line(f"br label %{end_label}")
        # Start an unreachable block so any code emitted by callers
        # (e.g. the if-then fallthrough br) remains structurally valid.
        dead = self._fresh_label("dead")
        self._emit_label(dead)
        return None

    def _emit_let(self, node: N.LetDecl) -> str | None:
        if node.type:
            ty = self._llvm_type(node.type)
            nyet_name = self._nyet_type_name(node.type)
        elif node.value:
            ty = self._infer_llvm_type(node.value)
            nyet_name = self._infer_nyet_type_name(node.value)
        else:
            ty = "i32"
            nyet_name = None

        # For struct/sum-type bindings, the value is already a ptr (from construction)
        is_aggregate = (
            nyet_name is not None
            and (nyet_name in self._structs or nyet_name in self._sum_types)
        )

        if is_aggregate:
            # Value is a ptr to the struct — store the ptr itself
            if node.value is not None:
                val = self._emit_expr(node.value)
                if val is not None:
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr {val}, ptr {ptr}")
                    self._env[node.name] = (ptr, "ptr")
                    self._env_struct_name[node.name] = nyet_name
            else:
                ptr = self._emit_alloca("ptr")
                self._env[node.name] = (ptr, "ptr")
        else:
            ptr = self._emit_alloca(ty)
            if node.value is not None:
                val = self._emit_expr(node.value)
                if val is not None:
                    val_ty = self._infer_llvm_type(node.value)
                    if self._is_float(ty) and not self._is_float(val_ty):
                        conv = self._fresh_tmp()
                        self._emit_line(f"{conv} = sitofp {val_ty} {val} to {ty}")
                        val = conv
                    self._emit_line(f"store {ty} {val}, ptr {ptr}")
            self._env[node.name] = (ptr, ty)

        if isinstance(node.value, N.StringLit):
            self._str_lits[node.name] = node.value.value
        return None

    def _infer_nyet_type_name(self, node: N.Node) -> str | None:
        """Try to infer the Nyet type name from an expression."""
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            name = node.head.name
            if name in self._structs:
                return name
            if name in self._variant_ctors:
                return self._variant_ctors[name][0]
        return None

    def _emit_assign(self, node: N.Assign) -> str | None:
        if isinstance(node.target, N.Ident) and node.target.name in self._env:
            ptr, ty = self._env[node.target.name]
            val = self._emit_expr(node.value)
            if val is not None:
                self._emit_line(f"store {ty} {val}, ptr {ptr}")
        return None

    # ------------------------------------------------------------------
    # User-defined function calls
    # ------------------------------------------------------------------

    def _emit_user_call(self, name: str, args: list[N.Expr]) -> str | None:
        arg_vals: list[tuple[str, str]] = []
        for arg in args:
            v = self._emit_expr(arg)
            if v is not None:
                ty = self._infer_llvm_type(arg)
                arg_vals.append((ty, v))

        # Use registered signature if available
        if name in self._fn_sigs:
            sig_types, ret_type = self._fn_sigs[name]
        else:
            ret_type = "i32"

        args_str = ", ".join(f"{t} {v}" for t, v in arg_vals)
        if ret_type == "void":
            self._emit_line(f"call void @{name}({args_str})")
            return None
        else:
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = call {ret_type} @{name}({args_str})")
            return tmp

    # ==================================================================
    # env_struct_name: tracks which Nyet struct/sum type a binding holds
    # ==================================================================

    @property
    def _env_struct_name(self) -> dict[str, str]:
        if not hasattr(self, "_esn"):
            self._esn: dict[str, str] = {}
        return self._esn


def emit_ir(program: list[N.Node], target_triple: str = "") -> str:
    emitter = Emitter(target_triple)
    return emitter.emit(program)
