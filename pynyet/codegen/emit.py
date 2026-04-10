"""LLVM IR text emitter for the v0.1 Nyet subset.

Walks the AST and emits LLVM IR as text strings (no llvmlite dependency).
The output can be compiled directly with `clang -o output output.ll`.

v0.1 scope: fn main, out (prints), string/int/float/bool literals,
arithmetic, comparisons, if, do, let/var bindings.
"""

from __future__ import annotations

from pynyet.ast import nodes as N


class Emitter:
    """Emit LLVM IR text from a Nyet AST."""

    def __init__(self, target_triple: str = "") -> None:
        self._triple = target_triple
        self._lines: list[str] = []
        # key → (global_name, byte_len, raw_bytes)
        self._strings: dict[str, tuple[str, int, bytes]] = {}
        self._fmt_i32: str | None = None
        self._fmt_f64: str | None = None
        self._tmp = 0  # SSA temp counter
        self._label = 0  # label counter
        self._env: dict[str, tuple[str, str]] = {}  # name → (llvm_ptr, llvm_type)
        self._declared_externs: set[str] = set()
        self._fn_lines: list[str] = []  # lines for current function body

    def emit(self, program: list[N.Node]) -> str:
        """Emit LLVM IR for a complete program. Returns IR text."""
        # Collect all top-level declarations
        for node in program:
            if isinstance(node, N.FnDecl):
                self._emit_fn(node)

        # Assemble final module
        out: list[str] = []
        if self._triple:
            out.append(f'target triple = "{self._triple}"')
            out.append("")

        # String constants
        for key, (name, byte_len, raw_bytes) in self._strings.items():
            escaped = self._escape_bytes(raw_bytes)
            out.append(f'{name} = private unnamed_addr constant [{byte_len} x i8] c"{escaped}"')

        if self._strings:
            out.append("")

        # Extern declarations
        for decl in sorted(self._declared_externs):
            out.append(decl)
        if self._declared_externs:
            out.append("")

        # Function definitions
        out.extend(self._lines)
        out.append("")
        return "\n".join(out)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fresh_tmp(self) -> str:
        self._tmp += 1
        return f"%t{self._tmp}"

    def _fresh_label(self, prefix: str = "L") -> str:
        self._label += 1
        return f"{prefix}{self._label}"

    def _emit_line(self, line: str) -> None:
        """Add a line to the current function body."""
        self._fn_lines.append(f"  {line}")

    def _emit_label(self, name: str) -> None:
        self._fn_lines.append(f"{name}:")

    def _get_string(self, value: str) -> tuple[str, int]:
        """Get or create a global string constant (null-terminated).

        Used with `puts` which adds a newline automatically.
        """
        key = f"str:{value}"
        if key in self._strings:
            name, byte_len, _ = self._strings[key]
            return name, byte_len
        idx = len(self._strings)
        name = f"@.str.{idx}"
        raw = value.encode("utf-8") + b"\x00"
        self._strings[key] = (name, len(raw), raw)
        return name, len(raw)

    def _get_format_string(self, fmt: str, key_suffix: str) -> str:
        """Get or create a printf format string constant. Returns global name."""
        key = f"fmt:{key_suffix}"
        if key in self._strings:
            return self._strings[key][0]
        idx = len(self._strings)
        name = f"@.str.{idx}"
        raw = fmt.encode("utf-8") + b"\x00"
        self._strings[key] = (name, len(raw), raw)
        return name

    def _declare_puts(self) -> None:
        decl = "declare i32 @puts(ptr)"
        self._declared_externs.add(decl)

    def _declare_printf(self) -> None:
        decl = "declare i32 @printf(ptr, ...)"
        self._declared_externs.add(decl)

    def _get_fmt_i32(self) -> str:
        """Get format string for printing an i32 with newline."""
        if self._fmt_i32 is None:
            self._fmt_i32 = self._get_format_string("%d\n", "i32")
        return self._fmt_i32

    def _get_fmt_f64(self) -> str:
        """Get format string for printing an f64 with newline."""
        if self._fmt_f64 is None:
            self._fmt_f64 = self._get_format_string("%g\n", "f64")
        return self._fmt_f64

    @staticmethod
    def _escape_bytes(data: bytes) -> str:
        """Escape raw bytes for an LLVM IR constant."""
        result = []
        for b in data:
            if 32 <= b < 127 and b not in (ord('"'), ord('\\')):
                result.append(chr(b))
            else:
                result.append(f"\\{b:02X}")
        return "".join(result)

    # ------------------------------------------------------------------
    # Top-level
    # ------------------------------------------------------------------

    def _emit_fn(self, node: N.FnDecl) -> None:
        """Emit a function definition."""
        self._tmp = 0
        self._label = 0
        self._fn_lines = []
        saved_env = dict(self._env)

        if node.name == "main":
            # C main convention: returns i32
            self._lines.append("define i32 @main() {")
            self._emit_label("entry")

            if node.body is not None:
                self._emit_expr(node.body)

            self._emit_line("ret i32 0")
        else:
            # Map params
            param_types, param_names = self._fn_param_types(node)
            ret_type = self._llvm_ret_type(node.return_type)
            params_str = ", ".join(
                f"{t} %{n}" for t, n in zip(param_types, param_names)
            )
            self._lines.append(f"define {ret_type} @{node.name}({params_str}) {{")
            self._emit_label("entry")

            # Alloca params so they're mutable
            for t, n in zip(param_types, param_names):
                ptr = self._fresh_tmp()
                self._emit_line(f"{ptr} = alloca {t}")
                self._emit_line(f"store {t} %{n}, ptr {ptr}")
                self._env[n] = (ptr, t)

            if node.body is not None:
                result = self._emit_expr(node.body)
                if ret_type == "void":
                    self._emit_line("ret void")
                elif result is not None:
                    self._emit_line(f"ret {ret_type} {result}")
                else:
                    self._emit_line("ret void")
            else:
                if ret_type == "void":
                    self._emit_line("ret void")
                else:
                    self._emit_line(f"ret {ret_type} 0")

        self._lines.extend(self._fn_lines)
        self._lines.append("}")
        self._lines.append("")
        self._env = saved_env

    def _fn_param_types(self, node: N.FnDecl) -> tuple[list[str], list[str]]:
        types = []
        names = []
        for p in node.params:
            types.append(self._llvm_type(p.type))
            names.append(p.name)
        return types, names

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
                return "ptr"  # pointer to char data
            if name in ("i8", "u8"):
                return "i8"
            if name in ("i16", "u16"):
                return "i16"
        if isinstance(tn, N.UnitType):
            return "void"
        return "i32"

    def _llvm_ret_type(self, tn: N.TypeNode | None) -> str:
        if tn is None:
            return "void"
        return self._llvm_type(tn)

    # ------------------------------------------------------------------
    # Expression emission — returns an LLVM SSA value (or None for void)
    # ------------------------------------------------------------------

    def _emit_expr(self, node: N.Node) -> str | None:
        if isinstance(node, N.IntLit):
            return str(node.value)

        if isinstance(node, N.FloatLit):
            # Use hex representation for exact LLVM double constants
            import struct
            packed = struct.pack("d", node.value)
            as_int = struct.unpack("Q", packed)[0]
            return f"0x{as_int:016X}"

        if isinstance(node, N.BoolLit):
            return "1" if node.value else "0"

        if isinstance(node, N.StringLit):
            name, byte_len = self._get_string(node.value)
            return name

        if isinstance(node, N.UnitLit):
            return None

        if isinstance(node, N.Pass):
            return None

        if isinstance(node, N.Ident):
            return self._emit_ident(node)

        if isinstance(node, N.Call):
            return self._emit_call(node)

        if isinstance(node, N.If):
            return self._emit_if(node)

        if isinstance(node, N.Do):
            return self._emit_do(node)

        if isinstance(node, N.LetDecl):
            return self._emit_let(node)

        if isinstance(node, N.Return):
            if node.value is not None:
                val = self._emit_expr(node.value)
                self._emit_line(f"ret i32 {val}")
            else:
                self._emit_line("ret void")
            return None

        if isinstance(node, N.Assign):
            return self._emit_assign(node)

        return None

    def _emit_ident(self, node: N.Ident) -> str | None:
        if node.name in self._env:
            ptr, ty = self._env[node.name]
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = load {ty}, ptr {ptr}")
            return tmp
        return None

    def _emit_call(self, node: N.Call) -> str | None:
        if isinstance(node.head, N.Ident):
            name = node.head.name
            # Built-in: out
            if name == "out":
                return self._emit_out(node.args)
            # Arithmetic operators
            if name in ("+", "-", "*", "/", "%"):
                return self._emit_arith(name, node.args)
            # Comparison operators
            if name in ("==", "!=", "<", ">", "<=", ">="):
                return self._emit_cmp(name, node.args)
            # Boolean operators
            if name in ("&&", "||", "!"):
                return self._emit_bool_op(name, node.args)
            # User-defined function call
            return self._emit_user_call(name, node.args)
        return None

    def _emit_out(self, args: list[N.Expr]) -> str | None:
        """Emit code for the `out` builtin."""
        for arg in args:
            if isinstance(arg, N.StringLit):
                self._declare_puts()
                name, _ = self._get_string(arg.value)
                tmp = self._fresh_tmp()
                self._emit_line(f"{tmp} = call i32 @puts(ptr {name})")
            elif isinstance(arg, N.IntLit):
                self._declare_printf()
                fmt = self._get_fmt_i32()
                tmp = self._fresh_tmp()
                self._emit_line(f"{tmp} = call i32 (ptr, ...) @printf(ptr {fmt}, i32 {arg.value})")
            else:
                val = self._emit_expr(arg)
                if val is not None:
                    ty = self._infer_llvm_type(arg)
                    if ty == "ptr":
                        self._declare_puts()
                        tmp = self._fresh_tmp()
                        self._emit_line(f"{tmp} = call i32 @puts(ptr {val})")
                    elif ty == "double":
                        self._declare_printf()
                        fmt = self._get_fmt_f64()
                        tmp = self._fresh_tmp()
                        self._emit_line(f"{tmp} = call i32 (ptr, ...) @printf(ptr {fmt}, double {val})")
                    else:
                        self._declare_printf()
                        fmt = self._get_fmt_i32()
                        tmp = self._fresh_tmp()
                        self._emit_line(f"{tmp} = call i32 (ptr, ...) @printf(ptr {fmt}, i32 {val})")
        return None

    def _infer_llvm_type(self, node: N.Node) -> str:
        """Infer the LLVM type of an expression node."""
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
                return "i32"
            if op in ("==", "!=", "<", ">", "<=", ">=", "&&", "||", "!"):
                return "i1"
        return "i32"

    def _emit_arith(self, op: str, args: list[N.Expr]) -> str | None:
        if len(args) < 2:
            return None
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            return None
        tmp = self._fresh_tmp()
        ops = {"+": "add", "-": "sub", "*": "mul", "/": "sdiv", "%": "srem"}
        self._emit_line(f"{tmp} = {ops[op]} i32 {lhs}, {rhs}")
        return tmp

    def _emit_cmp(self, op: str, args: list[N.Expr]) -> str | None:
        if len(args) < 2:
            return None
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            return None
        tmp = self._fresh_tmp()
        conds = {"==": "eq", "!=": "ne", "<": "slt", ">": "sgt",
                 "<=": "sle", ">=": "sge"}
        self._emit_line(f"{tmp} = icmp {conds[op]} i32 {lhs}, {rhs}")
        return tmp

    def _emit_bool_op(self, op: str, args: list[N.Expr]) -> str | None:
        if op == "!" and len(args) >= 1:
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
        if op == "&&":
            self._emit_line(f"{tmp} = and i1 {lhs}, {rhs}")
        else:  # ||
            self._emit_line(f"{tmp} = or i1 {lhs}, {rhs}")
        return tmp

    def _emit_if(self, node: N.If) -> str | None:
        cond = self._emit_expr(node.cond)
        if cond is None:
            return None
        then_label = self._fresh_label("then")
        else_label = self._fresh_label("else")
        end_label = self._fresh_label("ifend")

        # Allocate a slot for the result (if both branches produce values)
        result_ptr = self._fresh_tmp()
        self._emit_line(f"{result_ptr} = alloca ptr")

        self._emit_line(f"br i1 {cond}, label %{then_label}, label %{else_label}")

        self._emit_label(then_label)
        then_val = self._emit_expr(node.then_branch)
        if then_val is not None:
            self._emit_line(f"store ptr {then_val}, ptr {result_ptr}")
        self._emit_line(f"br label %{end_label}")

        self._emit_label(else_label)
        if node.else_branch is not None:
            else_val = self._emit_expr(node.else_branch)
            if else_val is not None:
                self._emit_line(f"store ptr {else_val}, ptr {result_ptr}")
        self._emit_line(f"br label %{end_label}")

        self._emit_label(end_label)
        if then_val is not None:
            result = self._fresh_tmp()
            self._emit_line(f"{result} = load ptr, ptr {result_ptr}")
            return result
        return None

    def _emit_do(self, node: N.Do) -> str | None:
        result = None
        for expr in node.exprs:
            result = self._emit_expr(expr)
        return result

    def _emit_let(self, node: N.LetDecl) -> str | None:
        if node.type:
            ty = self._llvm_type(node.type)
        elif node.value:
            ty = self._infer_llvm_type(node.value)
        else:
            ty = "i32"
        ptr = self._fresh_tmp()
        self._emit_line(f"{ptr} = alloca {ty}")
        if node.value is not None:
            val = self._emit_expr(node.value)
            if val is not None:
                self._emit_line(f"store {ty} {val}, ptr {ptr}")
        self._env[node.name] = (ptr, ty)
        return None

    def _emit_assign(self, node: N.Assign) -> str | None:
        if isinstance(node.target, N.Ident) and node.target.name in self._env:
            ptr, ty = self._env[node.target.name]
            val = self._emit_expr(node.value)
            if val is not None:
                self._emit_line(f"store {ty} {val}, ptr {ptr}")
        return None

    def _emit_user_call(self, name: str, args: list[N.Expr]) -> str | None:
        """Emit a call to a user-defined function."""
        arg_vals = []
        for arg in args:
            v = self._emit_expr(arg)
            if v is not None:
                arg_vals.append(("i32", v))  # assume i32 for now
        args_str = ", ".join(f"{t} {v}" for t, v in arg_vals)
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call i32 @{name}({args_str})")
        return tmp


def emit_ir(program: list[N.Node], target_triple: str = "") -> str:
    """Emit LLVM IR text for a parsed Nyet program."""
    emitter = Emitter(target_triple)
    return emitter.emit(program)
