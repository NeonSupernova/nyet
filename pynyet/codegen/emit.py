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
        # v0.3: nyet return-type names per fn — lets `?` / let bindings
        # recover the sum type when an expression is a fn call.
        self._fn_ret_nyet_names: dict[str, str] = {}

        # v0.2: sum type registry — name → [(variant_name, payload_types)]
        self._sum_types: dict[str, list[tuple[str, list[str]]]] = {}
        # variant_name → (sum_type_name, variant_index)
        self._variant_ctors: dict[str, tuple[str, int]] = {}
        # v0.7: parallel registry tracking the Nyet type name of each
        # payload field so nested patterns can recover the inner sum/struct
        # type when the LLVM type is just `ptr`. Indexed by sum name →
        # variant index → field index → Nyet type name (or None if not a
        # sum/struct type).
        self._sum_payload_nyet: dict[str, list[list[str | None]]] = {}

        # Array bindings — name → LLVM element type ("i32", "double", "ptr"…).
        # Set whenever a let/var/param resolves to `Array[T]`. Used by
        # `_emit_call` and `_emit_assign` to dispatch `(arr i)` and
        # `(= (arr i) v)` to indexed load/store instead of a function call.
        self._env_array_elem: dict[str, str] = {}

        # v0.3: generic fn templates — name → FnDecl (not yet emitted)
        self._fn_templates: dict[str, N.FnDecl] = {}
        # (fn_name, type_args_tuple) → mangled_name — already-monomorphized
        self._mono_fns: dict[tuple, str] = {}
        # v0.3: generic struct templates — name → StructDecl
        self._struct_templates: dict[str, N.StructDecl] = {}
        # v0.3: generic sum type templates — name → TypeDecl
        self._sum_templates: dict[str, N.TypeDecl] = {}
        # (type_name, type_args_tuple) → mangled_name — already-monomorphized
        self._mono_types: dict[tuple, str] = {}
        # Unqualified variant name → mangled sum type — set during monomorphization
        # E.g. Some (generic) has ctor entry when we monomorphize Option[i32] → Option__i32
        self._generic_variant_ctors: dict[str, set[str]] = {}

        # v0.4: operator overload registry — (struct_name, op) → mangled fn name.
        # Populated when `(impl Trait Type ...)` or `(impl Type ...)` declares
        # a method whose name is an operator (`+`, `==`, etc.). Looked up by
        # `_emit_arith` / `_emit_cmp` to dispatch through the impl.
        self._method_impls: dict[tuple[str, str], str] = {}

        # v0.6: lambda lifting state.
        # `_closure_counter`: monotonic id used to generate unique
        # `__closure_N` names when a `(fn ...)` literal is lifted to a
        # top-level function. `_env_fn_sig` is the per-function map of
        # binding-name → (param_llvm_types, ret_llvm_type) for any binding
        # that refers to a function (a fn-typed parameter, or a let bound
        # to a lifted closure / top-level fn). Calls go through it to
        # decide between a direct named `call @fn` and an indirect
        # `call %fnptr` through a function pointer.
        self._closure_counter: int = 0
        self._env_fn_sig: dict[str, tuple[list[str], str]] = {}

    # ==================================================================
    # v0.6: Closure lifting
    # ==================================================================

    def _lift_closures(self, program: list[N.Node]) -> list[N.Node]:
        """Hoist `(fn ...)` literals to top-level functions.

        Each `FnExpr` is replaced with an `Ident` referring to a fresh
        top-level `__closure_N` `FnDecl` appended to the program list.
        The lifted name is later treated as a function pointer at call
        sites (see `_emit_call`'s indirect-call path).

        Captures are not yet supported: any free variable in the lambda
        body will fail later as an undefined identifier. That's fine for
        the v0.6 milestone goal, which exercises non-capturing lambdas
        through higher-order functions.
        """
        lifted: list[N.FnDecl] = []
        new_program = [self._lift_in(n, lifted) for n in program]
        return new_program + lifted

    def _lift_in(self, node: N.Node, lifted: list[N.FnDecl]) -> N.Node:
        if node is None:
            return node

        if isinstance(node, N.FnExpr):
            # Recurse into the body first so nested lambdas are lifted too.
            inner_body = self._lift_in(node.body, lifted) if node.body else None
            name = f"__closure_{self._closure_counter}"
            self._closure_counter += 1
            decl = N.FnDecl(
                node.span, name, list(node.params), node.return_type, inner_body
            )
            lifted.append(decl)
            return N.Ident(node.span, name)

        if isinstance(node, N.FnDecl):
            if node.body is not None:
                node.body = self._lift_in(node.body, lifted)
            return node

        if isinstance(node, N.ImplDecl):
            node.items = [self._lift_in(it, lifted) for it in node.items]
            return node

        if isinstance(node, (N.LetDecl, N.ConstDecl)):
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        if isinstance(node, N.Call):
            if node.head is not None:
                node.head = self._lift_in(node.head, lifted)
            node.args = [self._lift_in(a, lifted) for a in node.args]
            return node

        if isinstance(node, N.Do):
            node.exprs = [self._lift_in(e, lifted) for e in node.exprs]
            return node

        if isinstance(node, N.If):
            if node.cond is not None:
                node.cond = self._lift_in(node.cond, lifted)
            if node.then_branch is not None:
                node.then_branch = self._lift_in(node.then_branch, lifted)
            if node.else_branch is not None:
                node.else_branch = self._lift_in(node.else_branch, lifted)
            return node

        if isinstance(node, N.Match):
            if node.scrutinee is not None:
                node.scrutinee = self._lift_in(node.scrutinee, lifted)
            for arm in node.arms:
                arm.body = self._lift_in(arm.body, lifted)
            return node

        if isinstance(node, N.Return):
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        if isinstance(node, N.Assign):
            if node.target is not None:
                node.target = self._lift_in(node.target, lifted)
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        if isinstance(node, N.Loop):
            if node.body is not None:
                node.body = self._lift_in(node.body, lifted)
            return node

        if isinstance(node, N.Break):
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        if isinstance(node, (N.ArrayLit, N.TupleLit)):
            node.elements = [self._lift_in(e, lifted) for e in node.elements]
            return node

        if isinstance(node, N.KeywordArg):
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        if isinstance(node, (N.Try, N.Await, N.Spawn)):
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        return node

    # ==================================================================
    # Public entry point
    # ==================================================================

    def emit(self, program: list[N.Node]) -> str:
        # v0.6: lambda-lift `(fn ...)` literals into auto-named top-level
        # functions. The pre-pass mutates the program list in place by
        # appending the lifted decls and substituting Ident references for
        # each FnExpr.
        program = self._lift_closures(program)

        fns: list[N.FnDecl] = []
        top_level: list[N.Node] = []
        has_main = False

        for node in program:
            if isinstance(node, N.StructDecl):
                if node.generics:
                    self._struct_templates[node.name] = node
                else:
                    self._register_struct(node)
            elif isinstance(node, N.TypeDecl):
                if node.generics:
                    self._sum_templates[node.name] = node
                    # Remember variant names so _emit_call can detect them
                    for vname, _ in node.variants:
                        self._generic_variant_ctors.setdefault(vname, set()).add(node.name)
                else:
                    self._register_sum_type(node)
            elif isinstance(node, N.FnDecl):
                if node.generics:
                    self._fn_templates[node.name] = node
                else:
                    fns.append(node)
                if node.name == "main":
                    has_main = True
            elif isinstance(node, N.ImplDecl):
                # Hoist impl methods to top-level fns. Inherent and operator
                # methods are both mangled per target type so multiple impls
                # of `display`, `+`, etc. can coexist. The mangled name is
                # registered in `_method_impls` for `_emit_call` /
                # `_emit_arith` / `_emit_cmp` / `_emit_out` dispatch.
                target_name = self._nyet_type_name(node.target)
                for item in node.items:
                    if not isinstance(item, N.FnDecl):
                        continue
                    if target_name is not None:
                        mangled = self._op_mangle(target_name, item.name)
                        self._method_impls[(target_name, item.name)] = mangled
                        item.name = mangled
                        if item.generics:
                            self._fn_templates[item.name] = item
                        else:
                            fns.append(item)
                    elif item.name.isidentifier():
                        if item.generics:
                            self._fn_templates[item.name] = item
                        else:
                            fns.append(item)
            else:
                top_level.append(node)

        # Pre-register every concrete function's signature so call sites
        # in bodies can reference fns regardless of declaration order
        # (matters for v0.6 lifted lambdas appended after `main`).
        for fn in fns:
            self._register_fn_sig(fn)

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
        payload_nyet: list[list[str | None]] = []
        max_payload = 0
        for vname, vtypes in node.variants:
            field_types = [self._llvm_type(t) for t in vtypes]
            nyet_names: list[str | None] = []
            for t in vtypes:
                n = self._nyet_type_name_of_node(t)
                if n is not None and (
                    n in self._sum_types
                    or n in self._sum_templates
                    or n in self._structs
                    or n in self._struct_templates
                ):
                    nyet_names.append(n)
                else:
                    nyet_names.append(None)
            payload_nyet.append(nyet_names)
            payload = sum(self._sizeof(t) for t in field_types)
            if payload > max_payload:
                max_payload = payload
            variants.append((vname, field_types))
            tag_idx = len(variants) - 1
            self._variant_ctors[vname] = (node.name, tag_idx)
            # v0.7: also register a composite key so nested-generic sum
            # types whose monomorphizations share variant names (e.g.
            # Option__i32::Some and Option__Option__i32::Some) can be
            # disambiguated at construction and pattern-test time.
            self._variant_ctors[f"{node.name}::{vname}"] = (node.name, tag_idx)

        self._sum_types[node.name] = variants
        self._sum_payload_nyet[node.name] = payload_nyet

        if max_payload == 0:
            # Pure enum (all unit variants)
            self._struct_type_lines.append(
                f"%{node.name} = type {{ i32 }}"
            )
        else:
            self._struct_type_lines.append(
                f"%{node.name} = type {{ i32, [{max_payload} x i8] }}"
            )

    # ==================================================================
    # v0.3: Monomorphization
    # ==================================================================

    @staticmethod
    def _mangle(name: str, type_args: tuple[str, ...]) -> str:
        if not type_args:
            return name
        return name + "__" + "_".join(type_args)

    def _nyet_type_name_of_node(self, tn: N.TypeNode | None) -> str | None:
        """Get the Nyet type name used for mangling from a TypeNode."""
        if isinstance(tn, (N.PrimType, N.NamedType)):
            return tn.name
        if isinstance(tn, N.GenericType):
            base = self._nyet_type_name_of_node(tn.base)
            if base is None:
                return None
            inner = [self._nyet_type_name_of_node(a) or "unk" for a in tn.args]
            return self._mangle(base, tuple(inner))
        return None

    def _llvm_of_nyet_name(self, name: str) -> str:
        """Map a Nyet type name (possibly mangled) to an LLVM type."""
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
        if name in self._structs or name in self._sum_types:
            return "ptr"
        return "i32"

    def _infer_nyet_type_from_arg(self, node: N.Node) -> str:
        """Infer a concrete Nyet type name for use as generic type arg."""
        if isinstance(node, N.IntLit):
            return "i32"
        if isinstance(node, N.FloatLit):
            return "f64"
        if isinstance(node, N.BoolLit):
            return "bool"
        if isinstance(node, N.StringLit):
            return "string"
        if isinstance(node, N.Ident) and node.name in self._env:
            ty = self._env[node.name][1]
            struct_name = self._env_struct_name.get(node.name)
            if struct_name:
                return struct_name
            return self._nyet_from_llvm(ty)
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            op = node.head.name
            if op in self._structs:
                return op
            if op in self._variant_ctors:
                return self._variant_ctors[op][0]
            if op in self._fn_sigs:
                ret = self._fn_sigs[op][1]
                return self._nyet_from_llvm(ret)
        ty = self._infer_llvm_type(node)
        return self._nyet_from_llvm(ty)

    @staticmethod
    def _nyet_from_llvm(ty: str) -> str:
        mapping = {
            "i32": "i32", "i64": "i64", "double": "f64", "float": "f32",
            "i1": "bool", "i8": "i8", "i16": "i16",
        }
        if ty in mapping:
            return mapping[ty]
        if ty == "ptr":
            return "string"  # default ptr → string for mangling
        return "i32"

    def _subst_type(
        self, tn: N.TypeNode | None, env: dict[str, N.TypeNode]
    ) -> N.TypeNode | None:
        """Substitute generic params in a TypeNode using env mapping."""
        if tn is None:
            return None
        if isinstance(tn, N.NamedType):
            if tn.name in env:
                return env[tn.name]
            return tn
        if isinstance(tn, N.PrimType):
            return tn
        if isinstance(tn, N.GenericType):
            new_base = self._subst_type(tn.base, env) or tn.base
            new_args = [self._subst_type(a, env) or a for a in tn.args]
            return N.GenericType(tn.span, new_base, new_args)
        if isinstance(tn, N.UnitType):
            return tn
        return tn

    def _subst_body(self, node: N.Node, env: dict[str, N.TypeNode]) -> N.Node:
        """Return a shallow clone of `node` with type annotations substituted.

        We only rewrite nodes that carry TypeNode fields relevant to codegen.
        """
        import copy as _copy

        if isinstance(node, N.LetDecl):
            n = _copy.copy(node)
            n.type = self._subst_type(node.type, env)
            if node.value is not None:
                n.value = self._subst_body(node.value, env)
            return n
        if isinstance(node, N.ConstDecl):
            n = _copy.copy(node)
            n.type = self._subst_type(node.type, env)
            if node.value is not None:
                n.value = self._subst_body(node.value, env)
            return n
        if isinstance(node, N.Do):
            n = _copy.copy(node)
            n.exprs = [self._subst_body(e, env) for e in node.exprs]
            return n
        if isinstance(node, N.If):
            n = _copy.copy(node)
            if node.cond is not None:
                n.cond = self._subst_body(node.cond, env)
            if node.then_branch is not None:
                n.then_branch = self._subst_body(node.then_branch, env)
            if node.else_branch is not None:
                n.else_branch = self._subst_body(node.else_branch, env)
            return n
        if isinstance(node, N.Match):
            n = _copy.copy(node)
            if node.scrutinee is not None:
                n.scrutinee = self._subst_body(node.scrutinee, env)
            n.arms = []
            for arm in node.arms:
                a = _copy.copy(arm)
                a.body = self._subst_body(arm.body, env)
                n.arms.append(a)
            return n
        if isinstance(node, N.Return):
            n = _copy.copy(node)
            if node.value is not None:
                n.value = self._subst_body(node.value, env)
            return n
        if isinstance(node, N.Call):
            n = _copy.copy(node)
            n.args = [self._subst_body(a, env) for a in node.args]
            return n
        if isinstance(node, N.Loop):
            n = _copy.copy(node)
            if node.body is not None:
                n.body = self._subst_body(node.body, env)
            return n
        if isinstance(node, N.Break):
            n = _copy.copy(node)
            if node.value is not None:
                n.value = self._subst_body(node.value, env)
            return n
        if isinstance(node, N.Assign):
            n = _copy.copy(node)
            if node.value is not None:
                n.value = self._subst_body(node.value, env)
            return n
        if isinstance(node, N.FieldAccess):
            n = _copy.copy(node)
            if node.target is not None:
                n.target = self._subst_body(node.target, env)
            return n
        return node

    def _monomorphize_fn(
        self, name: str, type_args: tuple[str, ...]
    ) -> str | None:
        """Emit a specialized copy of a generic fn and return the mangled name."""
        key = (name, type_args)
        if key in self._mono_fns:
            return self._mono_fns[key]
        tmpl = self._fn_templates.get(name)
        if tmpl is None:
            return None
        if len(type_args) != len(tmpl.generics):
            return None

        mangled = self._mangle(name, type_args)
        self._mono_fns[key] = mangled

        # Build substitution env: generic param name → concrete TypeNode
        env: dict[str, N.TypeNode] = {}
        for gp, targ in zip(tmpl.generics, type_args):
            env[gp.name] = N.NamedType(tmpl.span, targ)

        # Clone the fn decl
        import copy as _copy
        clone = _copy.copy(tmpl)
        clone.name = mangled
        clone.generics = []
        clone.params = []
        for p in tmpl.params:
            np = _copy.copy(p)
            np.type = self._subst_type(p.type, env)
            clone.params.append(np)
        clone.return_type = self._subst_type(tmpl.return_type, env)
        if tmpl.body is not None:
            clone.body = self._subst_body(tmpl.body, env)

        # Emit the specialized function
        self._emit_fn(clone)
        return mangled

    def _monomorphize_sum_type(
        self, name: str, type_args: tuple[str, ...]
    ) -> str | None:
        """Emit a specialized sum type and register it. Returns mangled name."""
        key = (name, type_args)
        if key in self._mono_types:
            return self._mono_types[key]
        tmpl = self._sum_templates.get(name)
        if tmpl is None:
            return None
        if len(type_args) != len(tmpl.generics):
            return None

        mangled = self._mangle(name, type_args)
        self._mono_types[key] = mangled

        env: dict[str, N.TypeNode] = {}
        for gp, targ in zip(tmpl.generics, type_args):
            env[gp.name] = N.NamedType(tmpl.span, targ)

        import copy as _copy
        clone = _copy.copy(tmpl)
        clone.name = mangled
        clone.generics = []
        clone.variants = []
        for vname, vtypes in tmpl.variants:
            subst_types = [self._subst_type(t, env) or t for t in vtypes]
            clone.variants.append((vname, subst_types))

        self._register_sum_type(clone)
        return mangled

    def _monomorphize_struct(
        self, name: str, type_args: tuple[str, ...]
    ) -> str | None:
        key = (name, type_args)
        if key in self._mono_types:
            return self._mono_types[key]
        tmpl = self._struct_templates.get(name)
        if tmpl is None:
            return None
        if len(type_args) != len(tmpl.generics):
            return None

        mangled = self._mangle(name, type_args)
        self._mono_types[key] = mangled

        env: dict[str, N.TypeNode] = {}
        for gp, targ in zip(tmpl.generics, type_args):
            env[gp.name] = N.NamedType(tmpl.span, targ)

        import copy as _copy
        clone = _copy.copy(tmpl)
        clone.name = mangled
        clone.generics = []
        clone.fields = []
        for p in tmpl.fields:
            np = _copy.copy(p)
            np.type = self._subst_type(p.type, env)
            clone.fields.append(np)

        self._register_struct(clone)
        return mangled

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
        if isinstance(tn, N.GenericType):
            # v0.3: instantiate on demand
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name == "Array":
                # Arrays lower to a heap pointer (header + elements).
                return "ptr"
            if base_name is not None:
                # Force inner generic args to monomorphize first so any
                # nested sum/struct types are registered before we use
                # their mangled names as a payload field type.
                for a in tn.args:
                    self._llvm_type(a)
                type_args = tuple(
                    self._nyet_type_name_of_node(a) or "unk" for a in tn.args
                )
                if base_name in self._sum_templates:
                    self._monomorphize_sum_type(base_name, type_args)
                    return "ptr"
                if base_name in self._struct_templates:
                    self._monomorphize_struct(base_name, type_args)
                    return "ptr"
                # Already-monomorphized: base might be the mangled name
                mangled = self._mangle(base_name, type_args)
                if mangled in self._structs or mangled in self._sum_types:
                    return "ptr"
        if isinstance(tn, N.RefType):
            # `&T` and `&!T` lower to the same shape as T for codegen.
            return self._llvm_type(tn.inner)
        if isinstance(tn, N.FnType):
            # Function pointer.
            return "ptr"
        if isinstance(tn, N.UnitType):
            return "void"
        return "i32"

    def _array_elem_llvm_type(self, tn: N.TypeNode | None) -> str | None:
        """Return the LLVM element type if `tn` is `Array[T]` (or `&Array[T]`)."""
        if tn is None:
            return None
        if isinstance(tn, N.RefType):
            return self._array_elem_llvm_type(tn.inner)
        if isinstance(tn, N.GenericType):
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name == "Array" and tn.args:
                return self._llvm_type(tn.args[0])
        return None

    def _llvm_ret_type(self, tn: N.TypeNode | None) -> str:
        if tn is None:
            return "void"
        return self._llvm_type(tn)

    def _nyet_type_name(self, tn: N.TypeNode | None) -> str | None:
        """Return the Nyet type name if it's a named/prim type.

        For generic instantiations, returns the mangled name (and
        triggers monomorphization if needed).
        """
        if isinstance(tn, N.RefType):
            return self._nyet_type_name(tn.inner)
        if isinstance(tn, (N.PrimType, N.NamedType)):
            return tn.name
        if isinstance(tn, N.GenericType):
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name is None:
                return None
            type_args = tuple(
                self._nyet_type_name_of_node(a) or "unk" for a in tn.args
            )
            if base_name in self._sum_templates:
                return self._monomorphize_sum_type(base_name, type_args)
            if base_name in self._struct_templates:
                return self._monomorphize_struct(base_name, type_args)
            mangled = self._mangle(base_name, type_args)
            if mangled in self._structs or mangled in self._sum_types:
                return mangled
            return None
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

    def _save_fn_state(self) -> dict:
        """Snapshot per-function emitter state (for re-entrant fn emission)."""
        return {
            "tmp": self._tmp,
            "label": self._label,
            "fn_lines": self._fn_lines,
            "fn_alloca_lines": self._fn_alloca_lines,
            "env": dict(self._env),
            "env_struct_name": dict(self._env_struct_name),
            "env_array_elem": dict(self._env_array_elem),
            "env_fn_sig": dict(self._env_fn_sig),
            "loop_stack": list(self._loop_stack),
            "str_lits": dict(self._str_lits),
        }

    def _restore_fn_state(self, saved: dict) -> None:
        self._tmp = saved["tmp"]
        self._label = saved["label"]
        self._fn_lines = saved["fn_lines"]
        self._fn_alloca_lines = saved["fn_alloca_lines"]
        self._env = saved["env"]
        self._env_struct_name.clear()
        self._env_struct_name.update(saved["env_struct_name"])
        self._env_array_elem = saved["env_array_elem"]
        self._env_fn_sig = saved["env_fn_sig"]
        self._loop_stack = saved["loop_stack"]
        self._str_lits = saved["str_lits"]

    def _register_fn_sig(self, node: N.FnDecl) -> None:
        """Pre-populate `_fn_sigs` so call sites resolve regardless of
        the order functions appear in the program list."""
        if node.name == "main":
            return
        param_types = [self._llvm_type(p.type) for p in node.params]
        ret_type = self._llvm_ret_type(node.return_type)
        self._fn_sigs[node.name] = (param_types, ret_type)
        ret_nyet = self._nyet_type_name(node.return_type) if node.return_type else None
        if ret_nyet:
            self._fn_ret_nyet_names[node.name] = ret_nyet

    def _emit_fn(self, node: N.FnDecl) -> None:
        saved = self._save_fn_state()
        self._tmp = 0
        self._label = 0
        self._fn_lines = []
        self._fn_alloca_lines = []

        # Emit into a local buffer, then flush to self._lines at end.
        body_lines: list[str] = []

        if node.name == "main":
            body_lines.append("define i32 @main() {")
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
            ret_nyet = self._nyet_type_name(node.return_type) if node.return_type else None
            if ret_nyet:
                self._fn_ret_nyet_names[node.name] = ret_nyet

            params_str = ", ".join(
                f"{t} %{n}" for t, n in zip(param_types, param_names)
            )
            body_lines.append(
                f"define {ret_type} @{node.name}({params_str}) {{"
            )
            self._emit_label("entry")

            for p, t, n, nyet_n in zip(
                node.params, param_types, param_names, param_nyet_names
            ):
                arr_elem = self._array_elem_llvm_type(p.type)
                if arr_elem is not None:
                    # Array params arrive as `ptr` to the heap block.
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    self._env_array_elem[n] = arr_elem
                elif isinstance(p.type, N.FnType):
                    # Function-typed param: a pointer to a function. Track its
                    # signature so calls like `(f x)` can be lowered as an
                    # indirect call through the slot.
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    fn_param_tys = [self._llvm_type(pt) for pt in p.type.params]
                    fn_ret_ty = (
                        self._llvm_ret_type(p.type.ret) if p.type.ret else "void"
                    )
                    self._env_fn_sig[n] = (fn_param_tys, fn_ret_ty)
                elif nyet_n and (nyet_n in self._structs or nyet_n in self._sum_types):
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

        # Splice this fn's body into body_lines, then append all at once
        if self._fn_lines:
            body_lines.append(self._fn_lines[0])  # "entry:"
            body_lines.extend(self._fn_alloca_lines)
            body_lines.extend(self._fn_lines[1:])
        body_lines.append("}")
        body_lines.append("")

        self._restore_fn_state(saved)
        self._lines.extend(body_lines)

    def _emit_implicit_main(self, stmts: list[N.Node]) -> None:
        saved = self._save_fn_state()
        self._tmp = 0
        self._label = 0
        self._fn_lines = []
        self._fn_alloca_lines = []
        body_lines: list[str] = []
        body_lines.append("define i32 @main() {")
        self._emit_label("entry")
        for stmt in stmts:
            self._emit_expr(stmt)
        self._emit_line("ret i32 0")
        if self._fn_lines:
            body_lines.append(self._fn_lines[0])
            body_lines.extend(self._fn_alloca_lines)
            body_lines.extend(self._fn_lines[1:])
        body_lines.append("}")
        body_lines.append("")
        self._restore_fn_state(saved)
        self._lines.extend(body_lines)

    def _splice_fn_lines(self) -> None:
        """Deprecated; kept for compatibility. Do nothing."""
        pass

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
        if isinstance(node, N.ArrayLit):
            return "ptr"
        if isinstance(node, N.Ident) and node.name in self._env:
            return self._env[node.name][1]
        # v0.6: a bare reference to a top-level fn yields its function pointer.
        if isinstance(node, N.Ident) and node.name in self._fn_sigs:
            return "ptr"
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            op = node.head.name
            # Array indexing: shadows any same-named function.
            if op in self._env_array_elem and len(node.args) == 1:
                return self._env_array_elem[op]
            if op in ("+", "-", "*", "/", "%"):
                if node.args:
                    sn = self._infer_nyet_type_name(node.args[0])
                    if sn is None and isinstance(node.args[0], N.Ident):
                        sn = self._env_struct_name.get(node.args[0].name)
                    if sn is not None and (sn, op) in self._method_impls:
                        mangled = self._method_impls[(sn, op)]
                        if mangled in self._fn_sigs:
                            return self._fn_sigs[mangled][1]
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
            if op in ("&", "&!") and len(node.args) == 1:
                return self._infer_llvm_type(node.args[0])
            if op == "fmt":
                return "ptr"
            if op in ("file_open", "file_read_all"):
                return "ptr"
            if op == "in":
                if node.args and isinstance(node.args[0], N.Ident):
                    return self._llvm_type_from_name(node.args[0].name)
                return "ptr"
            if op in self._structs or op in self._variant_ctors:
                return "ptr"
            if op in self._fn_sigs:
                return self._fn_sigs[op][1]
            # Inherent method dispatch — look up the receiver's struct.
            if node.args:
                probe = self._unwrap_borrow(node.args[0])
                sn = self._infer_nyet_type_name(probe)
                if sn is None and isinstance(probe, N.Ident):
                    sn = self._env_struct_name.get(probe.name)
                if sn is not None and (sn, op) in self._method_impls:
                    mangled = self._method_impls[(sn, op)]
                    if mangled in self._fn_sigs:
                        return self._fn_sigs[mangled][1]
            # v0.3: generic fn — infer type args, look up monomorphized sig
            if op in self._fn_templates:
                tmpl = self._fn_templates[op]
                type_args = self._infer_type_args_for_fn(tmpl, node.args)
                if type_args is not None:
                    mangled = self._mangle(op, type_args)
                    if mangled in self._fn_sigs:
                        return self._fn_sigs[mangled][1]
                    # Walk return type with substitution
                    env: dict[str, N.TypeNode] = {}
                    for gp, targ in zip(tmpl.generics, type_args):
                        env[gp.name] = N.NamedType(tmpl.span, targ)
                    rt = self._subst_type(tmpl.return_type, env)
                    return self._llvm_type(rt)
            # v0.3: generic struct / generic variant
            if op in self._struct_templates:
                return "ptr"
            if op in self._generic_variant_ctors:
                return "ptr"
        if isinstance(node, N.FieldAccess):
            return self._infer_field_type(node)
        if isinstance(node, N.If) and node.then_branch:
            return self._infer_llvm_type(node.then_branch)
        if isinstance(node, N.Do) and node.exprs:
            return self._infer_llvm_type(node.exprs[-1])
        if isinstance(node, N.Try):
            # Payload type of the first (success) variant of the sum type.
            sum_name = self._sum_name_of(node.value)
            if sum_name and sum_name in self._sum_types:
                variants = self._sum_types[sum_name]
                if variants:
                    _, payload_types = variants[0]
                    if payload_types:
                        return payload_types[0]
            return "ptr"
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

        if isinstance(node, N.ArrayLit):
            return self._emit_array_lit(node)

        if isinstance(node, N.Call):
            return self._emit_call(node)

        if isinstance(node, N.FieldAccess):
            return self._emit_field_access(node)

        if isinstance(node, N.If):
            return self._emit_if(node)

        if isinstance(node, N.Match):
            return self._emit_match(node)

        if isinstance(node, N.Try):
            return self._emit_try(node)

        if isinstance(node, N.Do):
            return self._emit_do(node)

        if isinstance(node, (N.LetDecl, N.ConstDecl)):
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
        # v0.6: lifted lambda / top-level function used as a value yields
        # the global function pointer.
        if node.name in self._fn_sigs:
            return f"@{node.name}"
        return None

    # ==================================================================
    # Calls — builtins, operators, struct ctors, user fns
    # ==================================================================

    def _emit_call(self, node: N.Call) -> str | None:
        if isinstance(node.head, N.Ident):
            name = node.head.name
            # Array indexing: `(arr i)` where `arr` is a local Array[T].
            # A bound name shadows any same-named function, matching scoping.
            if name in self._env_array_elem and len(node.args) == 1:
                return self._emit_array_index(name, node.args[0])
            # Builtins
            if name == "out":
                return self._emit_out(node.args)
            if name == "in":
                return self._emit_in(node.args)
            if name == "fmt":
                return self._emit_fmt(node.args)
            if name == "panic":
                return self._emit_panic(node.args)
            if name == "len" and len(node.args) == 1:
                return self._emit_array_len(node.args[0])
            if name == "file_open":
                return self._emit_file_open(node.args)
            if name == "file_read_all":
                return self._emit_file_read_all(node.args)
            if name == "file_write":
                return self._emit_file_write(node.args)
            if name == "file_close":
                return self._emit_file_close(node.args)
            # Operators
            if name in ("+", "-", "*", "/", "%"):
                return self._emit_arith(name, node.args)
            if name in ("==", "!=", "<", ">", "<=", ">="):
                return self._emit_cmp(name, node.args)
            if name in ("&&", "||", "!"):
                return self._emit_bool_op(name, node.args)
            # Borrow / mutable borrow — pass-through; struct/sum values are
            # already pointer-shaped, so `&x` is just `x`.
            if name in ("&", "&!") and len(node.args) == 1:
                return self._emit_expr(node.args[0])
            # Struct constructor (already monomorphized — matched directly)
            if name in self._structs:
                return self._emit_struct_construct(name, node.args)
            # v0.3: Generic sum type variant — pick instantiation from args.
            # Try this before the bare-name lookup because monomorphization
            # registers each variant under its plain name, which means the
            # last-registered sum type wins in `_variant_ctors[name]` for
            # multi-instantiation programs (e.g. Option[i32] AND
            # Option[Option[i32]]).
            if name in self._generic_variant_ctors:
                mangled_vname = self._resolve_generic_variant(name, node.args)
                if mangled_vname:
                    return self._emit_variant_construct(mangled_vname, node.args)
            # Sum type variant constructor (already monomorphized or
            # belonging to a non-generic sum type)
            if name in self._variant_ctors:
                return self._emit_variant_construct(name, node.args)
            # v0.3: Generic struct constructor
            if name in self._struct_templates:
                mangled = self._monomorphize_struct_from_args(name, node.args)
                if mangled:
                    return self._emit_struct_construct(mangled, node.args)
            # v0.3: Generic function call
            if name in self._fn_templates:
                mangled = self._monomorphize_fn_from_args(name, node.args)
                if mangled:
                    return self._emit_user_call(mangled, node.args)
            # Inherent method dispatch: `(method receiver ...)` —
            # look up the receiver's struct type and try its impl.
            if name not in self._fn_sigs and node.args:
                probe = self._unwrap_borrow(node.args[0])
                sn = self._infer_nyet_type_name(probe)
                if sn is None and isinstance(probe, N.Ident):
                    sn = self._env_struct_name.get(probe.name)
                if sn is not None and (sn, name) in self._method_impls:
                    return self._emit_user_call(
                        self._method_impls[(sn, name)], node.args
                    )
            # v0.6: indirect call through a fn-typed binding (parameter
            # or let bound to a lifted lambda).
            if name in self._env_fn_sig:
                return self._emit_indirect_call(name, node.args)
            # User function
            return self._emit_user_call(name, node.args)
        return None

    # ------------------------------------------------------------------
    # v0.3: Generic call inference helpers
    # ------------------------------------------------------------------

    def _monomorphize_fn_from_args(
        self, name: str, args: list[N.Expr]
    ) -> str | None:
        """Infer type args for a generic fn call and monomorphize."""
        tmpl = self._fn_templates[name]
        type_args = self._infer_type_args_for_fn(tmpl, args)
        if type_args is None:
            return None
        return self._monomorphize_fn(name, type_args)

    def _monomorphize_struct_from_args(
        self, name: str, args: list[N.Expr]
    ) -> str | None:
        tmpl = self._struct_templates[name]
        # Try to match field types with generic params
        type_args = self._infer_type_args_for_struct(tmpl, args)
        if type_args is None:
            return None
        return self._monomorphize_struct(name, type_args)

    def _resolve_generic_variant(
        self, vname: str, args: list[N.Expr]
    ) -> str | None:
        """Find the generic sum type this variant belongs to and monomorphize.

        Returns the mangled variant ctor name, or None if we can't infer.
        """
        sum_names = self._generic_variant_ctors.get(vname, set())
        if not sum_names:
            return None
        # Try each candidate sum type
        for sum_name in sum_names:
            tmpl = self._sum_templates.get(sum_name)
            if tmpl is None:
                continue
            # Find the variant definition
            variant_types: list[N.TypeNode] = []
            for vn, vtypes in tmpl.variants:
                if vn == vname:
                    variant_types = vtypes
                    break
            type_args = self._infer_type_args_for_variant(
                tmpl, variant_types, args
            )
            if type_args is None:
                continue
            mangled_sum = self._monomorphize_sum_type(sum_name, type_args)
            if mangled_sum is None:
                continue
            # The monomorphized variant name — since _register_sum_type
            # stores the original variant name keyed in _variant_ctors,
            # we need to use a per-instantiation mangled variant.
            # For simplicity: mangle variant names too.
            return self._find_variant_ctor_for(mangled_sum, vname)
        return None

    def _find_variant_ctor_for(
        self, sum_type_name: str, vname: str
    ) -> str | None:
        """Return the (possibly mangled) variant ctor key in _variant_ctors.

        Since _register_sum_type uses the short variant name, collisions
        can happen between different sum types sharing variant names.
        To avoid that, we key variant ctors by a mangled composite name
        `<sum_type>:<variant>` when the sum type has been monomorphized.
        """
        # Prefer the mangled form if registered
        composite = f"{sum_type_name}::{vname}"
        if composite in self._variant_ctors:
            return composite
        if vname in self._variant_ctors:
            return vname
        return None

    def _infer_type_args_for_fn(
        self, tmpl: N.FnDecl, args: list[N.Expr]
    ) -> tuple[str, ...] | None:
        return self._infer_type_args_from_params(
            tmpl.generics, tmpl.params, args
        )

    def _infer_type_args_for_struct(
        self, tmpl: N.StructDecl, args: list[N.Expr]
    ) -> tuple[str, ...] | None:
        # Filter keyword args out for positional matching
        pos_args: list[N.Expr] = []
        for a in args:
            if not isinstance(a, N.KeywordArg):
                pos_args.append(a)
        return self._infer_type_args_from_params(
            tmpl.generics, tmpl.fields, pos_args
        )

    def _infer_type_args_for_variant(
        self,
        tmpl: N.TypeDecl,
        variant_types: list[N.TypeNode],
        args: list[N.Expr],
    ) -> tuple[str, ...] | None:
        # Build fake params with the variant's field types
        fake_params = [
            N.Param(tmpl.span, f"_{i}", t)
            for i, t in enumerate(variant_types)
        ]
        return self._infer_type_args_from_params(
            tmpl.generics, fake_params, args
        )

    def _infer_type_args_from_params(
        self,
        generics: list[N.GenericParam],
        params: list,
        args: list[N.Expr],
    ) -> tuple[str, ...] | None:
        """Infer concrete type args by matching param types against arg types.

        For each generic param name, find the first param with that type name,
        then read the concrete type from the corresponding arg.
        """
        if not generics:
            return ()
        resolved: dict[str, str] = {}
        for i, p in enumerate(params):
            if i >= len(args):
                break
            ptype_name = self._nyet_type_name_of_node(p.type)
            if ptype_name is None:
                continue
            # Only bind unresolved generic names
            for gp in generics:
                if gp.name not in resolved and ptype_name == gp.name:
                    resolved[gp.name] = self._infer_nyet_type_from_arg(args[i])
        # Check all are resolved
        result = []
        for gp in generics:
            if gp.name not in resolved:
                return None
            result.append(resolved[gp.name])
        return tuple(result)

    # ------------------------------------------------------------------
    # out
    # ------------------------------------------------------------------

    def _emit_out(self, args: list[N.Expr]) -> str | None:
        for arg in args:
            sn = self._infer_nyet_type_name(self._unwrap_borrow(arg))
            if (
                sn is not None
                and sn in self._structs
                and (sn, "display") in self._method_impls
            ):
                mangled = self._method_impls[(sn, "display")]
                val = self._emit_user_call(mangled, [arg])
                if val is not None:
                    self._declare_printf()
                    fmt = self._get_fmt_str()
                    tmp = self._fresh_tmp()
                    self._emit_line(
                        f"{tmp} = call i32 (ptr, ...) "
                        f"@printf(ptr {fmt}, ptr {val})"
                    )
                continue
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
    # panic — print all args and exit(1). Terminates control flow.
    # ------------------------------------------------------------------

    def _emit_panic(self, args: list[N.Expr]) -> str | None:
        # Reuse the same per-type printf logic as `out` for the message.
        self._emit_out(args)
        # Append a trailing newline if no string arg likely contains one.
        # Cheap and consistent: always emit "\n" so panics stay legible.
        self._declare_printf()
        nl = self._get_format_string("\n", "panic_nl")
        nl_tmp = self._fresh_tmp()
        self._emit_line(
            f"{nl_tmp} = call i32 (ptr, ...) @printf(ptr {nl})"
        )
        self._declare_extern("declare void @exit(i32)")
        self._emit_line("call void @exit(i32 1)")
        self._emit_line("unreachable")
        # Open a fresh dead block so any following emission still has a
        # valid insertion point — LLVM rejects instructions after a
        # terminator within the same basic block.
        dead = self._fresh_label("after_panic")
        self._emit_label(dead)
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
    # File IO (v1.0): open / read_all / write / close.
    #
    # Handles are FILE* opaque pointers. Strings come in/out as null-
    # terminated `i8*` (the same shape `out`/`fmt` already produce).
    # ------------------------------------------------------------------

    def _emit_file_open(self, args: list[N.Expr]) -> str | None:
        if len(args) != 2:
            return None
        self._declare_extern("declare ptr @fopen(ptr, ptr)")
        path = self._emit_expr(args[0])
        mode = self._emit_expr(args[1])
        if path is None or mode is None:
            return None
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call ptr @fopen(ptr {path}, ptr {mode})")
        return tmp

    def _emit_file_read_all(self, args: list[N.Expr]) -> str | None:
        if len(args) != 1:
            return None
        self._declare_extern("declare i32 @fseek(ptr, i64, i32)")
        self._declare_extern("declare i64 @ftell(ptr)")
        self._declare_extern("declare void @rewind(ptr)")
        self._declare_extern("declare i64 @fread(ptr, i64, i64, ptr)")
        self._declare_extern("declare ptr @malloc(i64)")
        h = self._emit_expr(args[0])
        if h is None:
            return None
        seek_rc = self._fresh_tmp()
        self._emit_line(
            f"{seek_rc} = call i32 @fseek(ptr {h}, i64 0, i32 2)"
        )
        size = self._fresh_tmp()
        self._emit_line(f"{size} = call i64 @ftell(ptr {h})")
        self._emit_line(f"call void @rewind(ptr {h})")
        size_p1 = self._fresh_tmp()
        self._emit_line(f"{size_p1} = add i64 {size}, 1")
        buf = self._fresh_tmp()
        self._emit_line(f"{buf} = call ptr @malloc(i64 {size_p1})")
        nread = self._fresh_tmp()
        self._emit_line(
            f"{nread} = call i64 @fread(ptr {buf}, i64 1, i64 {size}, ptr {h})"
        )
        end = self._fresh_tmp()
        self._emit_line(f"{end} = getelementptr i8, ptr {buf}, i64 {size}")
        self._emit_line(f"store i8 0, ptr {end}")
        return buf

    def _emit_file_write(self, args: list[N.Expr]) -> str | None:
        if len(args) != 2:
            return None
        self._declare_extern("declare i64 @strlen(ptr)")
        self._declare_extern("declare i64 @fwrite(ptr, i64, i64, ptr)")
        h = self._emit_expr(args[0])
        text = self._emit_expr(args[1])
        if h is None or text is None:
            return None
        n = self._fresh_tmp()
        self._emit_line(f"{n} = call i64 @strlen(ptr {text})")
        wrote = self._fresh_tmp()
        self._emit_line(
            f"{wrote} = call i64 @fwrite(ptr {text}, i64 1, i64 {n}, ptr {h})"
        )
        return None

    def _emit_file_close(self, args: list[N.Expr]) -> str | None:
        if len(args) != 1:
            return None
        self._declare_extern("declare i32 @fclose(ptr)")
        h = self._emit_expr(args[0])
        if h is None:
            return None
        rc = self._fresh_tmp()
        self._emit_line(f"{rc} = call i32 @fclose(ptr {h})")
        return None

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

        buf_ptr = self._fresh_tmp()
        self._emit_line(f"{buf_ptr} = call ptr @malloc(i64 1024)")
        self._declare_extern("declare ptr @malloc(i64)")
        snprintf_args = f"ptr {buf_ptr}, i32 1024, ptr {fmt_name}"
        for llvm_ty, val in fmt_args:
            if llvm_ty == "float":
                # Variadic callee (snprintf %g) expects double — promote
                ext = self._fresh_tmp()
                self._emit_line(f"{ext} = fpext float {val} to double")
                val = ext
                llvm_ty = "double"
            snprintf_args += f", {llvm_ty} {val}"
        tmp = self._fresh_tmp()
        self._emit_line(
            f"{tmp} = call i32 (ptr, i32, ptr, ...) @snprintf({snprintf_args})"
        )
        return buf_ptr

    # ------------------------------------------------------------------
    # Arithmetic (type-aware: int or float)
    # ------------------------------------------------------------------

    @staticmethod
    def _op_mangle(struct_name: str, op: str) -> str:
        """Generate a unique LLVM-safe name for an impl method (operator or named)."""
        op_words = {
            "+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod",
            "==": "eq", "!=": "ne", "<": "lt", "<=": "le", ">": "gt",
            ">=": "ge", "&&": "and", "||": "or", "!": "not",
        }
        if op in op_words:
            return f"{struct_name}__op__{op_words[op]}"
        return f"{struct_name}__{op}"

    @staticmethod
    def _unwrap_borrow(node: N.Node) -> N.Node:
        """Peel off `(& x)` / `(&! x)` so we can inspect the inner expression."""
        while (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Ident)
            and node.head.name in ("&", "&!")
            and len(node.args) == 1
        ):
            node = node.args[0]
        return node

    def _maybe_dispatch_op_impl(
        self, op: str, args: list[N.Expr]
    ) -> str | None:
        """If args[0] is a struct with an op impl, call it and return result."""
        if not args:
            return None
        probe = self._unwrap_borrow(args[0])
        struct_name = self._infer_nyet_type_name(probe)
        if struct_name is None and isinstance(probe, N.Ident):
            struct_name = self._env_struct_name.get(probe.name)
        if struct_name is None:
            return None
        mangled = self._method_impls.get((struct_name, op))
        if mangled is None:
            return None
        return self._emit_user_call(mangled, args)

    def _emit_arith(self, op: str, args: list[N.Expr]) -> str | None:
        if len(args) < 2:
            return None
        dispatched = self._maybe_dispatch_op_impl(op, args)
        if dispatched is not None:
            return dispatched
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
        dispatched = self._maybe_dispatch_op_impl(op, args)
        if dispatched is not None:
            return dispatched
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

    def _heap_alloc_struct(self, llvm_struct_name: str, size_bytes: int) -> str:
        """Malloc-allocate a struct; returns the ptr."""
        self._declare_extern("declare ptr @malloc(i64)")
        ptr = self._fresh_tmp()
        self._emit_line(f"{ptr} = call ptr @malloc(i64 {size_bytes})")
        return ptr

    def _struct_size_bytes(self, name: str) -> int:
        """Approximate struct size (sum of field sizes, rounded up to 8)."""
        fields = self._structs.get(name, [])
        total = sum(self._sizeof(ty) for _, ty in fields)
        if total == 0:
            return 8
        # Round up to multiple of 8 for alignment
        return (total + 7) & ~7

    def _sum_type_size_bytes(self, name: str) -> int:
        """Size of a sum type = tag + max payload."""
        variants = self._sum_types.get(name, [])
        max_payload = 0
        for _, types in variants:
            payload = sum(self._sizeof(t) for t in types)
            if payload > max_payload:
                max_payload = payload
        total = 4 + max_payload  # i32 tag + payload
        return (total + 7) & ~7

    def _emit_struct_construct(self, name: str, args: list[N.Expr]) -> str:
        """Emit `(StructName field:val ...)` → malloc + store fields.

        Uses malloc instead of alloca so the struct survives the
        callee's stack frame being popped (enables returning structs).
        """
        fields = self._structs[name]
        ptr = self._heap_alloc_struct(name, self._struct_size_bytes(name))

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
        """Emit `(VariantName payload...)` → malloc sum type + store tag + payload.

        Uses malloc so sum types can be safely returned from functions.
        """
        sum_name, tag_idx = self._variant_ctors[vname]
        variants = self._sum_types[sum_name]
        _, payload_types = variants[tag_idx]

        ptr = self._heap_alloc_struct(sum_name, self._sum_type_size_bytes(sum_name))

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
        """Emit a match expression.

        v0.7: linear-test scheme — each arm emits a refutable test that
        branches to the next arm on failure and falls through with bindings
        on success. This uniformly supports nested VariantPats, GuardedPats,
        LitPats nested in payloads, and wildcards/variables.
        """
        scrut = self._emit_expr(node.scrutinee)
        if scrut is None:
            return None

        # Determine the sum type of the scrutinee
        sum_name = self._sum_name_of(node.scrutinee)
        if sum_name is None and isinstance(node.scrutinee, N.Ident):
            sum_name = self._env_struct_name.get(node.scrutinee.name)

        if sum_name is None or sum_name not in self._sum_types:
            # Not a sum type — fall back to value-based matching
            return self._emit_match_simple(scrut, node)

        end_label = self._fresh_label("match_end")
        result_ty = self._infer_llvm_type(node.arms[0].body) if node.arms else "i32"
        result_ptr = self._emit_alloca(result_ty)

        self._exhaustiveness_warn(node, sum_name)

        for arm in node.arms:
            next_label = self._fresh_label("match_next")
            pat = arm.pattern
            guard: N.Expr | None = None
            if isinstance(pat, N.GuardedPat):
                guard = pat.guard
                pat = pat.inner

            saved_env = dict(self._env)
            saved_struct = dict(self._env_struct_name)
            saved_array = dict(self._env_array_elem)
            saved_fnsig = dict(self._env_fn_sig)

            self._emit_pattern_test_sum(scrut, sum_name, pat, next_label)

            if guard is not None:
                guard_val = self._emit_expr(guard)
                if guard_val is not None:
                    body_label = self._fresh_label("match_body")
                    self._emit_line(
                        f"br i1 {guard_val}, label %{body_label}, label %{next_label}"
                    )
                    self._emit_label(body_label)

            body_val = self._emit_expr(arm.body)
            if body_val is not None:
                self._emit_line(f"store {result_ty} {body_val}, ptr {result_ptr}")
            self._emit_line(f"br label %{end_label}")

            self._env = saved_env
            self._env_struct_name.clear()
            self._env_struct_name.update(saved_struct)
            self._env_array_elem = saved_array
            self._env_fn_sig = saved_fnsig

            self._emit_label(next_label)

        # Fallthrough when no arm matched: just branch to end. result_ptr
        # is left at its undef alloca state, matching v0.2 behavior.
        self._emit_line(f"br label %{end_label}")

        self._emit_label(end_label)
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {result_ty}, ptr {result_ptr}")
        return result

    def _emit_pattern_test_sum(
        self,
        scrut_val: str,
        sum_name: str,
        pat: N.Pattern,
        fail_label: str,
    ) -> None:
        """Emit a refutable test against a scrutinee that is a pointer to
        a sum type struct. Branches to fail_label on mismatch; falls
        through with bindings populated on success."""
        if isinstance(pat, N.WildPat):
            return
        if isinstance(pat, N.VarPat):
            vptr = self._emit_alloca("ptr")
            self._emit_line(f"store ptr {scrut_val}, ptr {vptr}")
            self._env[pat.name] = (vptr, "ptr")
            self._env_struct_name[pat.name] = sum_name
            return
        if isinstance(pat, N.VariantPat):
            # Prefer the composite key so nested-generic sum types pick
            # the right variant.
            composite = f"{sum_name}::{pat.name}"
            ctor = self._variant_ctors.get(composite) or self._variant_ctors.get(
                pat.name
            )
            if ctor is None:
                self._emit_line(f"br label %{fail_label}")
                dead = self._fresh_label("after_dead")
                self._emit_label(dead)
                return
            ctor_sum, tag_idx = ctor
            actual_sum = ctor_sum if ctor_sum in self._sum_types else sum_name

            tag_ptr = self._fresh_tmp()
            self._emit_line(
                f"{tag_ptr} = getelementptr inbounds %{actual_sum}, "
                f"ptr {scrut_val}, i32 0, i32 0"
            )
            tag = self._fresh_tmp()
            self._emit_line(f"{tag} = load i32, ptr {tag_ptr}")
            cmp = self._fresh_tmp()
            self._emit_line(f"{cmp} = icmp eq i32 {tag}, {tag_idx}")
            ok_label = self._fresh_label("tag_ok")
            self._emit_line(
                f"br i1 {cmp}, label %{ok_label}, label %{fail_label}"
            )
            self._emit_label(ok_label)

            variants = self._sum_types[actual_sum]
            _, payload_types = variants[tag_idx]
            payload_nyet = self._sum_payload_nyet.get(actual_sum, [])
            nyet_names: list[str | None] = (
                payload_nyet[tag_idx] if tag_idx < len(payload_nyet) else []
            )

            if pat.args and payload_types:
                payload_ptr = self._fresh_tmp()
                self._emit_line(
                    f"{payload_ptr} = getelementptr inbounds %{actual_sum}, "
                    f"ptr {scrut_val}, i32 0, i32 1"
                )
                offset = 0
                for pi, ppat in enumerate(pat.args):
                    if pi >= len(payload_types):
                        break
                    pty = payload_types[pi]
                    inner_nyet = (
                        nyet_names[pi] if pi < len(nyet_names) else None
                    )
                    if offset == 0:
                        fld_ptr = payload_ptr
                    else:
                        fld_ptr = self._fresh_tmp()
                        self._emit_line(
                            f"{fld_ptr} = getelementptr i8, "
                            f"ptr {payload_ptr}, i32 {offset}"
                        )
                    val = self._fresh_tmp()
                    self._emit_line(f"{val} = load {pty}, ptr {fld_ptr}")
                    self._emit_pattern_test_value(
                        val, pty, inner_nyet, ppat, fail_label
                    )
                    offset += self._sizeof(pty)
            return
        if isinstance(pat, N.LitPat):
            # Match a literal against the whole sum struct — treat as fail.
            self._emit_line(f"br label %{fail_label}")
            dead = self._fresh_label("after_dead")
            self._emit_label(dead)
            return
        # TuplePat / StructPat against a sum scrutinee: not supported.
        self._emit_line(f"br label %{fail_label}")
        dead = self._fresh_label("after_dead")
        self._emit_label(dead)

    def _emit_pattern_test_value(
        self,
        val: str,
        llvm_ty: str,
        nyet_name: str | None,
        pat: N.Pattern,
        fail_label: str,
    ) -> None:
        """Emit a refutable test against a value (already loaded). For
        nested sum-typed payloads, `val` is a `ptr` and `nyet_name` is the
        sum type's mangled name."""
        if isinstance(pat, N.WildPat):
            return
        if isinstance(pat, N.VarPat):
            vptr = self._emit_alloca(llvm_ty)
            self._emit_line(f"store {llvm_ty} {val}, ptr {vptr}")
            self._env[pat.name] = (vptr, llvm_ty)
            if nyet_name is not None:
                self._env_struct_name[pat.name] = nyet_name
            return
        if isinstance(pat, N.LitPat):
            cmp_val = self._emit_expr(pat.value)
            if cmp_val is None:
                self._emit_line(f"br label %{fail_label}")
                dead = self._fresh_label("after_dead")
                self._emit_label(dead)
                return
            cmp = self._fresh_tmp()
            if llvm_ty in ("double", "float"):
                self._emit_line(
                    f"{cmp} = fcmp oeq {llvm_ty} {val}, {cmp_val}"
                )
            else:
                self._emit_line(
                    f"{cmp} = icmp eq {llvm_ty} {val}, {cmp_val}"
                )
            ok_label = self._fresh_label("lit_ok")
            self._emit_line(
                f"br i1 {cmp}, label %{ok_label}, label %{fail_label}"
            )
            self._emit_label(ok_label)
            return
        if isinstance(pat, N.VariantPat):
            # Nested sum match. Prefer the carrier-derived sum name so
            # `Some(Some x)` against Option[Option[i32]] resolves the
            # inner Some to Option[i32], not the outer one.
            inner_sum = nyet_name
            if inner_sum is None or inner_sum not in self._sum_types:
                ctor = self._variant_ctors.get(pat.name)
                if ctor is not None:
                    inner_sum = ctor[0]
            if inner_sum is None or inner_sum not in self._sum_types:
                self._emit_line(f"br label %{fail_label}")
                dead = self._fresh_label("after_dead")
                self._emit_label(dead)
                return
            self._emit_pattern_test_sum(val, inner_sum, pat, fail_label)
            return
        # TuplePat / StructPat / GuardedPat against a value: not supported
        # at this depth (guards live on whole arms).
        self._emit_line(f"br label %{fail_label}")
        dead = self._fresh_label("after_dead")
        self._emit_label(dead)

    def _exhaustiveness_warn(self, node: N.Match, sum_name: str) -> None:
        """Print a warning if the match doesn't cover every variant of
        `sum_name` and has no catch-all (Wild/Var/guardless) arm."""
        if sum_name not in self._sum_types:
            return
        variants = self._sum_types[sum_name]
        all_names = {v[0] for v in variants}
        covered: set[str] = set()
        has_catchall = False
        for arm in node.arms:
            pat = arm.pattern
            if isinstance(pat, N.GuardedPat):
                # A guarded arm is conditional — doesn't guarantee coverage.
                continue
            if isinstance(pat, (N.WildPat, N.VarPat)):
                has_catchall = True
                break
            if isinstance(pat, N.VariantPat) and pat.name in all_names:
                covered.add(pat.name)
        if has_catchall:
            return
        missing = sorted(all_names - covered)
        if missing:
            import sys
            print(
                f"warning: non-exhaustive match on {sum_name}; "
                f"missing variants: {', '.join(missing)}",
                file=sys.stderr,
            )

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
    # ? (Try) operator
    # ------------------------------------------------------------------

    def _emit_try(self, node: N.Try) -> str | None:
        """(? expr) — Option/Result early-return.

        For Option[T]: if Some(v), value is v; if None, return the None.
        For Result[T, E]: if Ok(v), value is v; if Err(e), return the Err.
        Convention: variant at tag 0 is success, tag 1 is failure.
        The failure path re-returns the original scrutinee pointer (which
        the enclosing fn must be declared to return).
        """
        scrut_val = self._emit_expr(node.value)
        if scrut_val is None:
            return None

        # Determine the sum type of the scrutinee
        sum_name = self._sum_name_of(node.value)
        if sum_name is None or sum_name not in self._sum_types:
            # Fallback: just return the value as-is (no-op ?)
            return scrut_val

        variants = self._sum_types[sum_name]
        if len(variants) < 2:
            return scrut_val

        # Load the tag
        tag_ptr = self._fresh_tmp()
        self._emit_line(
            f"{tag_ptr} = getelementptr inbounds %{sum_name}, "
            f"ptr {scrut_val}, i32 0, i32 0"
        )
        tag = self._fresh_tmp()
        self._emit_line(f"{tag} = load i32, ptr {tag_ptr}")

        # success if tag == 0
        is_ok = self._fresh_tmp()
        self._emit_line(f"{is_ok} = icmp eq i32 {tag}, 0")

        ok_label = self._fresh_label("try_ok")
        fail_label = self._fresh_label("try_fail")
        self._emit_line(
            f"br i1 {is_ok}, label %{ok_label}, label %{fail_label}"
        )

        # Failure path: return the scrutinee
        self._emit_label(fail_label)
        self._emit_line(f"ret ptr {scrut_val}")

        # Success path: extract payload of variant 0
        self._emit_label(ok_label)
        _, payload_types = variants[0]
        if not payload_types:
            # Unit success variant — return None equivalent (ptr)
            return scrut_val

        payload_ptr = self._fresh_tmp()
        self._emit_line(
            f"{payload_ptr} = getelementptr inbounds %{sum_name}, "
            f"ptr {scrut_val}, i32 0, i32 1"
        )
        first_type = payload_types[0]
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {first_type}, ptr {payload_ptr}")
        return result

    def _sum_name_of(self, node: N.Node) -> str | None:
        """Return the (possibly mangled) sum type name for a scrutinee."""
        if isinstance(node, N.Ident):
            return self._env_struct_name.get(node.name)
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            name = node.head.name
            if name in self._variant_ctors:
                return self._variant_ctors[name][0]
            if name in self._generic_variant_ctors:
                # Try to resolve via current call args
                mangled = self._resolve_generic_variant(name, node.args)
                if mangled and mangled in self._variant_ctors:
                    return self._variant_ctors[mangled][0]
            if name in self._fn_ret_nyet_names:
                rn = self._fn_ret_nyet_names[name]
                if rn in self._sum_types:
                    return rn
            if name in self._fn_templates:
                # Generic fn — resolve via args, then use its mangled ret name
                mangled = self._monomorphize_fn_from_args(name, node.args)
                if mangled and mangled in self._fn_ret_nyet_names:
                    rn = self._fn_ret_nyet_names[mangled]
                    if rn in self._sum_types:
                        return rn
        if isinstance(node, N.Try):
            # Chained ?: the payload of the first variant is itself a sum
            inner = self._sum_name_of(node.value)
            if inner and inner in self._sum_types:
                variants = self._sum_types[inner]
                if variants:
                    _, payload_types = variants[0]
                    if payload_types:
                        # If the first variant's payload is the sum type itself
                        # (rare), we fall through. Otherwise, we don't know.
                        return None
        return None

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

        # Array bindings: store the heap pointer and remember its element
        # type so `(name i)` and `(= (name i) v)` lower correctly. We
        # detect via either a `Array[T]` annotation or an `ArrayLit` rhs.
        elem_ty: str | None = None
        if node.type is not None:
            elem_ty = self._array_elem_llvm_type(node.type)
        if elem_ty is None and isinstance(node.value, N.ArrayLit) and node.value.elements:
            elem_ty = self._infer_llvm_type(node.value.elements[0])
        if elem_ty is not None and isinstance(node.value, N.ArrayLit):
            ptr = self._emit_alloca("ptr")
            val = self._emit_expr(node.value)
            if val is not None:
                self._emit_line(f"store ptr {val}, ptr {ptr}")
            else:
                self._emit_line(f"store ptr null, ptr {ptr}")
            self._env[node.name] = (ptr, "ptr")
            self._env_array_elem[node.name] = elem_ty
            return None

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

        # v0.6: if the let binds to a function (lifted lambda or top-level
        # fn name), copy the signature over so `(<name> args...)` later
        # dispatches as an indirect call through the slot.
        sig = self._fn_sig_of_value(node.value)
        if sig is not None:
            self._env_fn_sig[node.name] = sig
        return None

    def _fn_sig_of_value(
        self, value: N.Node | None
    ) -> tuple[list[str], str] | None:
        """If `value` is an Ident referring to a known function, return its
        (param_llvm_types, ret_llvm_type) signature. Otherwise None."""
        if value is None:
            return None
        if isinstance(value, N.Ident):
            if value.name in self._fn_sigs:
                return self._fn_sigs[value.name]
            if value.name in self._env_fn_sig:
                return self._env_fn_sig[value.name]
        return None

    def _infer_nyet_type_name(self, node: N.Node) -> str | None:
        """Try to infer the Nyet type name from an expression."""
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            name = node.head.name
            if name in self._structs:
                return name
            if name in self._variant_ctors:
                return self._variant_ctors[name][0]
            if name in self._fn_ret_nyet_names:
                return self._fn_ret_nyet_names[name]
            if node.args:
                probe = self._unwrap_borrow(node.args[0])
                sn = None
                if isinstance(probe, N.Ident) and probe.name in self._env_struct_name:
                    sn = self._env_struct_name[probe.name]
                if sn is None:
                    sn = self._infer_nyet_type_name(probe)
                if sn is not None and (sn, name) in self._method_impls:
                    mangled = self._method_impls[(sn, name)]
                    if mangled in self._fn_ret_nyet_names:
                        return self._fn_ret_nyet_names[mangled]
        if isinstance(node, N.Ident) and node.name in self._env_struct_name:
            return self._env_struct_name[node.name]
        return None

    def _emit_assign(self, node: N.Assign) -> str | None:
        target = node.target
        # Indexed assignment: `(= (arr i) v)`.
        if (
            isinstance(target, N.Call)
            and isinstance(target.head, N.Ident)
            and target.head.name in self._env_array_elem
            and len(target.args) == 1
        ):
            return self._emit_array_assign(
                target.head.name, target.args[0], node.value
            )
        if isinstance(target, N.Ident) and target.name in self._env:
            ptr, ty = self._env[target.name]
            val = self._emit_expr(node.value)
            if val is not None:
                self._emit_line(f"store {ty} {val}, ptr {ptr}")
        return None

    # ------------------------------------------------------------------
    # Array literals, indexing, and indexed assignment
    # ------------------------------------------------------------------
    #
    # In-memory layout for an `Array[T]`:
    #     [0:8]   i64 length
    #     [8:..]  N elements of T, packed (alignment matches T since the
    #             header is 8 bytes and `_sizeof(T)` is a power of two).
    # The Nyet value of an array is a `ptr` to this heap block, allocated
    # via `malloc`.

    def _array_data_base(self, arr_ptr: str) -> str:
        """Skip the 8-byte length header to land on element 0."""
        base = self._fresh_tmp()
        self._emit_line(f"{base} = getelementptr i8, ptr {arr_ptr}, i64 8")
        return base

    def _idx_to_i64(self, idx_val: str, idx_ty: str) -> str:
        """Promote an index value to i64 for pointer arithmetic."""
        if idx_ty == "i64":
            return idx_val
        idx64 = self._fresh_tmp()
        self._emit_line(f"{idx64} = sext {idx_ty} {idx_val} to i64")
        return idx64

    def _emit_array_len(self, arg: N.Expr) -> str:
        """Read the i64 length stored at offset 0 of an array's heap block,
        then truncate to i32 so it can be used in `i32` arithmetic and
        comparisons without explicit casts."""
        target = self._unwrap_borrow(arg)
        # Find the slot holding the array pointer.
        arr_ptr: str | None = None
        if isinstance(target, N.Ident) and target.name in self._env:
            slot, _ = self._env[target.name]
            arr_ptr = self._fresh_tmp()
            self._emit_line(f"{arr_ptr} = load ptr, ptr {slot}")
        else:
            arr_ptr = self._emit_expr(target)
        if arr_ptr is None:
            return "0"
        len64 = self._fresh_tmp()
        self._emit_line(f"{len64} = load i64, ptr {arr_ptr}")
        len32 = self._fresh_tmp()
        self._emit_line(f"{len32} = trunc i64 {len64} to i32")
        return len32

    def _emit_array_lit(self, node: N.ArrayLit) -> str:
        """Lower `[e0 e1 ... eN]` to malloc + length + element stores."""
        n = len(node.elements)
        elem_ty = (
            self._infer_llvm_type(node.elements[0]) if node.elements else "i32"
        )
        elem_size = self._sizeof(elem_ty)
        total = 8 + n * elem_size

        self._declare_extern("declare ptr @malloc(i64)")
        arr_ptr = self._fresh_tmp()
        self._emit_line(f"{arr_ptr} = call ptr @malloc(i64 {total})")
        self._emit_line(f"store i64 {n}, ptr {arr_ptr}")

        if n > 0:
            base = self._array_data_base(arr_ptr)
            for i, elem in enumerate(node.elements):
                val = self._emit_expr(elem)
                if val is None:
                    continue
                if i == 0:
                    slot = base
                else:
                    slot = self._fresh_tmp()
                    self._emit_line(
                        f"{slot} = getelementptr {elem_ty}, ptr {base}, i64 {i}"
                    )
                self._emit_line(f"store {elem_ty} {val}, ptr {slot}")
        return arr_ptr

    def _emit_array_index(self, name: str, idx_arg: N.Expr) -> str:
        ptr_slot, _ = self._env[name]
        elem_ty = self._env_array_elem[name]
        arr = self._fresh_tmp()
        self._emit_line(f"{arr} = load ptr, ptr {ptr_slot}")

        idx_val = self._emit_expr(idx_arg)
        idx_ty = self._infer_llvm_type(idx_arg)
        idx64 = self._idx_to_i64(idx_val or "0", idx_ty)

        base = self._array_data_base(arr)
        elem_ptr = self._fresh_tmp()
        self._emit_line(
            f"{elem_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {idx64}"
        )
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {elem_ty}, ptr {elem_ptr}")
        return result

    def _emit_array_assign(
        self, name: str, idx_arg: N.Expr, value: N.Expr | None
    ) -> str | None:
        ptr_slot, _ = self._env[name]
        elem_ty = self._env_array_elem[name]
        arr = self._fresh_tmp()
        self._emit_line(f"{arr} = load ptr, ptr {ptr_slot}")

        idx_val = self._emit_expr(idx_arg)
        idx_ty = self._infer_llvm_type(idx_arg)
        idx64 = self._idx_to_i64(idx_val or "0", idx_ty)

        if value is None:
            return None
        val = self._emit_expr(value)
        if val is None:
            return None

        # Coerce int→float when storing into a float-element array.
        val_ty = self._infer_llvm_type(value)
        if self._is_float(elem_ty) and not self._is_float(val_ty):
            conv = self._fresh_tmp()
            self._emit_line(f"{conv} = sitofp {val_ty} {val} to {elem_ty}")
            val = conv

        base = self._array_data_base(arr)
        elem_ptr = self._fresh_tmp()
        self._emit_line(
            f"{elem_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {idx64}"
        )
        self._emit_line(f"store {elem_ty} {val}, ptr {elem_ptr}")
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

    def _emit_indirect_call(self, name: str, args: list[N.Expr]) -> str | None:
        """Call through a fn-pointer binding (v0.6 closure / fn parameter).

        Loads the function pointer from the binding's slot, then issues
        an indirect `call` using the binding's stored signature.
        """
        sig_types, ret_type = self._env_fn_sig[name]
        ptr, _slot_ty = self._env[name]
        fnptr = self._fresh_tmp()
        self._emit_line(f"{fnptr} = load ptr, ptr {ptr}")

        arg_vals: list[tuple[str, str]] = []
        for i, arg in enumerate(args):
            v = self._emit_expr(arg)
            if v is None:
                continue
            ty = sig_types[i] if i < len(sig_types) else self._infer_llvm_type(arg)
            arg_vals.append((ty, v))
        args_str = ", ".join(f"{t} {v}" for t, v in arg_vals)
        if ret_type == "void":
            self._emit_line(f"call void {fnptr}({args_str})")
            return None
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call {ret_type} {fnptr}({args_str})")
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
