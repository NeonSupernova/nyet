"""LLVM IR text emitter for the Nyet compiler.

Walks the AST and emits LLVM IR as text strings (no llvmlite dependency).
The output can be compiled directly with `clang -o output output.ll`.

v0.2 scope: structs, field access, float arithmetic, type-aware calls,
sum types, match expressions — on top of v0.1 (fn, out, in, fmt, let,
var, if, do, arithmetic, comparisons).
"""

from __future__ import annotations

import struct as _struct
import sys as _sys
from collections.abc import Callable

from pynyet.ast import nodes as N
from pynyet.sema.borrow import compute_drop_names


class Emitter:
    """Emit LLVM IR text from a Nyet AST."""

    _UNSIGNED_NAMES = frozenset({"u8", "u16", "u32", "u64", "usize"})

    def __init__(self, target_triple: str = "") -> None:
        self._triple = target_triple
        self._lines: list[str] = []
        self._strings: dict[str, tuple[str, int, bytes]] = {}
        # fn name -> local struct-typed binding names to `free` at the
        # function's natural end-of-body fallthrough (drop insertion;
        # see compute_drop_names). Deliberately NOT inserted at explicit
        # `return`/`?`-operator early-exit points: a binding declared
        # later in the body wouldn't be initialized yet at an earlier
        # return, and this pass doesn't do position-aware liveness
        # analysis -- only "reached the natural end, so everything
        # unconditionally declared at the top level definitely ran".
        self._drop_names: dict[str, list[str]] = {}
        self._current_fn_name: str | None = None
        # The current function's declared `-> Array[T]` element LLVM
        # type, if any -- see `_emit_expr_as_array`.
        self._current_fn_array_ret_elem_ty: str | None = None
        # The current function's raw declared return TypeNode, if any --
        # see `_resolve_generic_variant`'s use of it to recover type args
        # a generic sum type variant's own arguments can't fully infer
        # (e.g. `Err`'s payload type in `(fn f () -> Result[T E] (Ok
        # v))`, which never appears in `Ok`'s own field types at all).
        self._current_fn_return_type_node: N.TypeNode | None = None
        # Keyword literals (`:name`) intern to a small integer ID, assigned
        # on first use — allocation-free, compared by identity via plain
        # i32 equality.
        self._keyword_ids: dict[str, int] = {}
        self._fmt_i32: str | None = None
        self._fmt_i64: str | None = None
        self._fmt_f64: str | None = None
        self._fmt_u32: str | None = None
        self._fmt_u64: str | None = None
        self._fmt_char: str | None = None
        self._tmp = 0
        self._label = 0
        self._env: dict[str, tuple[str, str]] = {}  # name → (llvm_ptr, llvm_type)
        # Top-level `const` bindings — name → (llvm_immediate, llvm_type).
        # Consts have no runtime address (see main.no's Constants section:
        # "inlined at every use site"), so this is checked directly by
        # _emit_ident/_infer_llvm_type instead of going through _env.
        self._const_values: dict[str, tuple[str, str]] = {}
        self._str_lits: dict[str, str] = {}
        self._declared_externs: set[str] = set()
        # Compiler-synthesized helper function definitions, keyed by
        # symbol name -- see `_get_or_emit_op_fn`.
        self._synth_fns: dict[str, str] = {}
        # Counter for hidden array locals -- see `_hof_array_arg`.
        self._hof_tmp_count = 0
        # fn name -> per-param LLVM scalar type for `&!T` scalar params
        # (None for every other param) -- see `_mut_ref_scalar_type`.
        self._fn_param_mut_ref: dict[str, list[str | None]] = {}
        # fn name -> LLVM element type T for a fn declared `-> Array[T]`.
        self._fn_ret_array_elem: dict[str, str] = {}
        # (tuple type name, field index) -> tuple type name of that field,
        # for tuples nested inside tuples -- see `_record_nested_tuple_fields`.
        self._tuple_field_tuple: dict[tuple[str, int], str] = {}
        # Lifted lambda name -> (captured names, capture mode) -- see `_lift_in`.
        self._closure_info: dict[str, tuple[list[str], N.CaptureMode]] = {}
        # Lifted lambda name -> one (name, llvm type, by_ref, binding metadata)
        # per capture, snapshotted where the closure is created.
        self._closure_capture_meta: dict[str, list[tuple[str, str, bool, dict]]] = {}
        # Lifted lambda name -> LLVM literal struct type of its closure record.
        self._closure_layout: dict[str, str] = {}
        # fn name -> (param llvm types, ret llvm type) of a fn-typed return.
        self._fn_ret_fn_sig: dict[str, tuple[list[str], str]] = {}
        # Names that are never function locals -- see `_collect_global_names`.
        self._lift_globals: set[str] = set()
        # Lifted lambda name -> its FnDecl -- see `_infer_untyped_lambda`.
        self._lifted_decls: dict[str, N.FnDecl] = {}
        # Lifted lambdas not emitted yet -- see `_emit_closures`.
        self._pending_closures: list[N.FnDecl] = []
        self._fn_lines: list[str] = []
        # Stack of (end_label, result_ptr | None, result_ty | None) for loop/break
        self._loop_stack: list[list] = []
        # Alloca instructions hoisted to the function entry block
        self._fn_alloca_lines: list[str] = []

        # v0.2: struct registry — name → [(field_name, llvm_type)]
        self._structs: dict[str, list[tuple[str, str]]] = {}
        self._struct_type_lines: list[str] = []
        # struct name → set of field names whose Nyet type is `string`
        # (as opposed to some other `ptr`-shaped field — nested struct,
        # Array, tuple, Map). Used by `_is_string_operand` so `(. row
        # name)` is recognized as a string for `==`/`</`>` comparisons.
        self._struct_string_fields: dict[str, set[str]] = {}
        # struct name → {field name: Nyet type name}, for every field
        # whose type is a named type (primitives included). Used by
        # `_struct_name_of`/`_infer_nyet_type_name` so `(let i (. o
        # inner))` (binding a nested struct field with no `&T`
        # annotation) keeps tracking which struct `i` is — without
        # this, `(. i val)` on it silently produced no field load, the
        # same bug already fixed for array/map reads and chained
        # indexing (see _env_array_elem_nyet/_env_map_val_nyet).
        self._struct_field_nyet: dict[str, dict[str, str]] = {}
        # struct name → {field name: element LLVM type}, for every field
        # whose type is `Array[T]`. Lets `_emit_struct_construct` route
        # an `(array_new n)` field initializer through
        # `_emit_expr_as_array` -- see `_register_struct`.
        self._struct_field_array_elem: dict[str, dict[str, str]] = {}

        # v0.2: fn signature registry — name → (param_types, ret_type)
        self._fn_sigs: dict[str, tuple[list[str], str]] = {}
        # v0.3: nyet return-type names per fn — lets `?` / let bindings
        # recover the sum type when an expression is a fn call.
        self._fn_ret_nyet_names: dict[str, str] = {}
        # Same idea, for a fn returning a tuple type (`#(T1 T2)`):
        # tuples are structurally typed (synthesized anonymous struct
        # names keyed by element LLVM types, not a user-given name like
        # a struct/sum type), so `_fn_ret_nyet_names` — which only ever
        # holds a Nyet type NAME — can't represent one. Without this,
        # `(let p (make_pair))` (no `#(T1 T2)` annotation) never
        # registered `p` in `_env_tuple_types`, so `(p 0)` was parsed as
        # an ordinary call to an undefined function named `p` instead of
        # a tuple index.
        self._fn_ret_tuple_types: dict[str, str] = {}

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
        # Parallel to `_env_array_elem`, but the *Nyet* element type name
        # (e.g. "Point") when `T` is a named type — only ever set when
        # `_env_array_elem[name]` is "ptr" and the element is a known
        # struct/sum type. Without this, `(let a (arr i))` (no `&T`
        # annotation) loses track of which struct `a` is, so `(. a
        # field)` silently fails: `_infer_nyet_type_name` had no case for
        # "a Call indexing a known array binding" at all, so `_emit_let`
        # took the scalar-`ptr` path instead of the aggregate path and
        # never registered `_env_struct_name[a]`.
        self._env_array_elem_nyet: dict[str, str] = {}
        # Same idea, for an Array[T] whose elements are themselves
        # closures/fn pointers (mirrors `_env_map_val_fn_sig`'s reasoning
        # for the Map case) -- (param_llvm_types, ret_llvm_type), so
        # `(let h (arr i)) (h ...)` recognizes `h` as callable.
        self._env_array_elem_fn_sig: dict[str, tuple[list[str], str]] = {}
        # Same idea again, for an Array[T] whose elements are themselves
        # arrays (e.g. `Array[Array[i32]]`, a 2D grid) -- name -> the
        # INNER array's LLVM element type. Without this, `(let row (grid
        # i))` (no annotation) registered `row` as an opaque scalar
        # `ptr` binding instead of an array one, so `(row j)` was parsed
        # as a call to an undefined function literally named `row` --
        # the same failure shape `_env_array_elem_nyet`/
        # `_env_array_elem_fn_sig` already fixed for struct/closure
        # elements, but arrays have no Nyet type NAME to key off (they're
        # structurally, not nominally, typed), hence a separate registry
        # rather than reusing `_env_array_elem_nyet`.
        self._env_array_elem_of_array: dict[str, str] = {}

        # Char bindings — names of variables whose Nyet type is `char`.
        # Used by _emit_cast to detect char→int conversions at the call site.
        self._env_char_names: set[str] = set()

        # String bindings — names of variables whose Nyet type is `string`.
        # Set whenever a let/var/param resolves to `string`. Used by
        # `_emit_call` to dispatch `(str i)` to an indexed byte load (a
        # `char`) instead of a function call, mirroring `_env_array_elem`.
        self._env_string_names: set[str] = set()

        # Unsigned-integer bindings — names of let/var/params whose Nyet
        # type is one of u8/u16/u32/u64/usize. LLVM integer types carry
        # no signedness of their own (i8/i16/i32/i64 are used for both
        # signed and unsigned Nyet types alike); this is the only place
        # that distinction survives past `_llvm_type`, so every op that
        # behaves differently for unsigned values (widening casts,
        # comparisons, division/remainder, printing) needs to consult
        # it via `_node_is_unsigned` instead of just the LLVM type.
        self._env_unsigned_names: set[str] = set()

        # Tuple bindings — name -> synthesized tuple struct type name
        # (see `_get_or_register_tuple_type`). Used by `_emit_call` to
        # dispatch `(t i)` to indexed field load, mirroring
        # `_env_array_elem`. Distinct tuple element-type signatures each
        # get their own anonymous struct type, registered on demand.
        self._env_tuple_types: dict[str, str] = {}
        self._tuple_types: dict[tuple[str, ...], str] = {}

        # Map[string V] bindings — name -> value LLVM type. Backed by
        # runtime/map.c's nyet_map_* functions; keys are always strings
        # (the runtime hashes/compares C strings). Used by `_emit_call`
        # to dispatch `(m key)` to a lookup and by `_emit_assign` to
        # dispatch `(= (m key) v)` to insert/update, mirroring
        # `_env_array_elem`.
        self._env_map_val_ty: dict[str, str] = {}
        # Parallel to `_env_map_val_ty`, but the *Nyet* value type name
        # (e.g. "Point") when `V` is a named type — mirrors
        # `_env_array_elem_nyet`'s reasoning: without this, `(let a (m
        # key))` (no `&T` annotation) loses track of which struct `a`
        # is, so `(. a field)` on it silently produces no field load.
        self._env_map_val_nyet: dict[str, str] = {}
        # Same idea, for a Map[K V] whose values are closures/fn
        # pointers (a dispatch-table pattern: `{"go" (fn () -> unit
        # ...)}`) -- (param_llvm_types, ret_llvm_type), so `(let h (m
        # key)) (h ...)` recognizes `h` as callable instead of `(h)`
        # being parsed as a call to an undefined function `h`.
        self._env_map_val_fn_sig: dict[str, tuple[list[str], str]] = {}

        # dyn Trait objects: `&dyn Trait` params are a 2-word fat
        # pointer `{data, vtable}`. `_traits` holds each trait's method
        # FnDecls in declaration order (the vtable slot layout);
        # `_vtables` caches one global constant array of function
        # pointers per (concrete_type, trait) pair; `_dyn_types` caches
        # the synthesized fat-pointer struct type per trait;
        # `_env_dyn_trait` tracks which local bindings/params are dyn
        # (name -> trait name), mirroring `_env_struct_name`;
        # `_fn_param_dyn_traits` records which of a function's
        # parameters are dyn so call sites know to coerce their
        # argument into a fat pointer.
        self._traits: dict[str, list[N.FnDecl]] = {}
        self._vtables: dict[tuple[str, str], str] = {}
        self._dyn_types: dict[str, str] = {}
        self._env_dyn_trait: dict[str, str] = {}
        self._fn_param_dyn_traits: dict[str, list[str | None]] = {}
        # Parallel to `_fn_param_dyn_traits`: which of a function's
        # parameters are `Array[T]`, and T's LLVM element type -- lets a
        # call site route a bare `(array_new n)` ARGUMENT through
        # `_emit_expr_as_array` instead of the plain `_emit_expr` that
        # can't infer array_new's element type from its own bare integer
        # argument alone.
        self._fn_param_array_elem: dict[str, list[str | None]] = {}

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

        A lambda's captures are the names it uses that it doesn't bind
        itself and that aren't global (functions, types, variants, consts,
        builtins) -- i.e. locals of an enclosing function. See
        `_emit_closure_value` for how the closure record is built where the
        lambda appears, and `_bind_closure_captures` for how the lifted
        function reads its captures back.
        """
        self._lift_globals = self._collect_global_names(program)
        lifted: list[N.FnDecl] = []
        new_program = [self._lift_in(n, lifted) for n in program]
        return new_program + lifted

    def _collect_global_names(self, program: list[N.Node]) -> set[str]:
        """Every name that can't be a function-local binding."""
        from pynyet.sema.resolve import BUILTINS, PRIM_TYPE_NAMES

        names: set[str] = set(BUILTINS) | set(PRIM_TYPE_NAMES) | {"true", "false", "self", "_"}
        for node in program:
            if isinstance(node, (N.FnDecl, N.StructDecl, N.ConstDecl, N.LetDecl)):
                names.add(node.name)
            elif isinstance(node, N.TypeDecl):
                names.add(node.name)
                names.update(vname for vname, _ in node.variants)
            elif isinstance(node, (N.TraitDecl, N.ImplDecl)):
                if isinstance(node, N.TraitDecl):
                    names.add(node.name)
                names.update(item.name for item in node.items if isinstance(item, N.FnDecl))
        return names

    def _free_names(self, fn: N.FnDecl) -> list[str]:
        """Locals of an enclosing function that lifted lambda `fn` refers to."""
        used: set[str] = set()
        bound: set[str] = {p.name for p in fn.params}
        self._scan_names(fn.body, used, bound)
        return sorted(
            name
            for name in used - bound - self._lift_globals
            if name.isidentifier() and not name.startswith("__closure_")
        )

    def _scan_names(self, node: object, used: set[str], bound: set[str]) -> None:
        if isinstance(node, (list, tuple)):
            for item in node:
                self._scan_names(item, used, bound)
            return
        if not isinstance(node, N.Node):
            return
        if isinstance(node, N.Ident):
            used.add(node.name)
            # A nested lambda (already lifted to an Ident by now) is created
            # inside this one, so whatever it captures must be available
            # here too.
            if node.name in self._closure_info:
                used.update(self._closure_info[node.name][0])
        elif isinstance(node, (N.LetDecl, N.ConstDecl, N.VarPat, N.Param)):
            bound.add(node.name)
        for value in vars(node).values():
            self._scan_names(value, used, bound)

    def _lift_in(self, node: N.Node, lifted: list[N.FnDecl]) -> N.Node:
        if node is None:
            return node

        if isinstance(node, N.FnExpr):
            # Recurse into the body first so nested lambdas are lifted too.
            inner_body = self._lift_in(node.body, lifted) if node.body else None
            name = f"__closure_{self._closure_counter}"
            self._closure_counter += 1
            decl = N.FnDecl(node.span, name, list(node.params), node.return_type, inner_body)
            lifted.append(decl)
            self._closure_info[name] = (self._free_names(decl), node.capture_mode)
            self._lifted_decls[name] = decl
            return N.Ident(node.span, name)

        if isinstance(node, N.FnDecl):
            # A generic function's lambdas mention its type parameters, so
            # they're lifted per instantiation instead -- see `_monomorphize_fn`.
            if node.body is not None and not node.generics:
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

        if isinstance(node, N.MapLit):
            # A closure literal as a map value (e.g. a `{name (fn ...)}`
            # dispatch table) was never recursed into -- this is an
            # explicit node-type whitelist, not a generic walk, and
            # `MapLit` was simply missing. The `FnExpr` reached
            # `_emit_expr` un-lifted and hit its catch-all.
            node.entries = [
                (self._lift_in(k, lifted), self._lift_in(v, lifted)) for k, v in node.entries
            ]
            return node

        if isinstance(node, N.KeywordArg):
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        if isinstance(node, (N.Try, N.Await, N.Spawn)):
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        if isinstance(node, N.FieldAccess):
            if node.target is not None:
                node.target = self._lift_in(node.target, lifted)
            return node

        if isinstance(node, N.Cast):
            if node.value is not None:
                node.value = self._lift_in(node.value, lifted)
            return node

        return node

    # ==================================================================
    # Public entry point
    # ==================================================================

    def emit(self, program: list[N.Node]) -> str:
        # Drop insertion (struct-only, see compute_drop_names' docstring):
        # computed against the same pre-lift program driver.py's own
        # check_borrows call already analyzed, so this sees exactly what
        # the borrow checker saw.
        self._drop_names = compute_drop_names(program)

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
            elif isinstance(node, N.TraitDecl):
                # Method order here is the vtable slot layout for `dyn
                # Trait` dispatch -- see _emit_dyn_call.
                self._traits[node.name] = [
                    item for item in node.items if isinstance(item, N.FnDecl)
                ]
            elif isinstance(node, N.ConstDecl):
                # Registered up front (not appended to top_level) so a
                # const is available to every function regardless of
                # whether `main` exists — see _register_const.
                self._register_const(node)
            else:
                top_level.append(node)

        self._register_builtin_channel_structs()

        # Pre-register every concrete function's signature so call sites
        # in bodies can reference fns regardless of declaration order
        # (matters for v0.6 lifted lambdas appended after `main`).
        for fn in fns:
            self._register_fn_sig(fn)

        for fn in fns:
            if fn.name not in self._closure_info:
                self._emit_fn(fn)
        self._pending_closures.extend(fn for fn in fns if fn.name in self._closure_info)

        if not has_main and top_level:
            self._emit_implicit_main(top_level)
        self._emit_closures()

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
        for _key, (name, byte_len, raw_bytes) in self._strings.items():
            escaped = self._escape_bytes(raw_bytes)
            out.append(f'{name} = private unnamed_addr constant [{byte_len} x i8] c"{escaped}"')
        if self._strings:
            out.append("")

        # Extern declarations
        for decl in sorted(self._declared_externs):
            out.append(decl)
        if self._declared_externs:
            out.append("")

        # Compiler-synthesized helper functions (see `_get_or_emit_op_fn`).
        for text in self._synth_fns.values():
            out.append(text)

        out.extend(self._lines)
        out.append("")
        return "\n".join(out)

    # ==================================================================
    # Struct / sum type registration
    # ==================================================================

    def _register_struct(self, node: N.StructDecl) -> None:
        fields = []
        string_fields: set[str] = set()
        field_nyet: dict[str, str] = {}
        field_array_elem: dict[str, str] = {}
        for p in node.fields:
            ty = self._llvm_type(p.type)
            fields.append((p.name, ty))
            nyet_name = self._nyet_type_name(p.type)
            if nyet_name == "string":
                string_fields.add(p.name)
            if nyet_name is not None:
                field_nyet[p.name] = nyet_name
            arr_elem = self._array_elem_llvm_type(p.type)
            if arr_elem is not None:
                field_array_elem[p.name] = arr_elem
        self._structs[node.name] = fields
        self._struct_string_fields[node.name] = string_fields
        self._struct_field_nyet[node.name] = field_nyet
        # `Array[T]`-typed fields, keyed by field name -> T's LLVM type --
        # see `_emit_struct_construct`'s use of it to route an
        # `(array_new n)` field initializer through `_emit_expr_as_array`.
        self._struct_field_array_elem[node.name] = field_array_elem
        llvm_fields = ", ".join(ty for _, ty in fields)
        self._struct_type_lines.append(f"%{node.name} = type {{ {llvm_fields} }}")

    def _register_builtin_channel_structs(self) -> None:
        """`FileIO` is a builtin struct implementing `IOChannel` (see the
        design plan) with no real `N.StructDecl` in Nyet source, so it's
        registered here directly rather than via `_register_struct`.
        `write`/`read`/`close` are hand-rolled emitters (below) that
        `_emit_call` dispatches to directly by name+receiver-type, ahead
        of the generic `_method_impls` inherent-method-dispatch path —
        registering them in `_method_impls` too keeps `(write &!f ...)`
        callable through the ordinary generic path as well (used when
        `f`'s static type can only be recovered that way, e.g. via
        `_env_struct_name`), pointing at the same sentinel names
        `_emit_call` special-cases; `_emit_user_call` is never actually
        reached for them.

        `StdIO` (the `out`/`in`/`err` builtin channel values) has no
        registration here at all — per the design plan, those three are
        resolved by static AST identity in `_emit_io`, never materialized
        as a real runtime value, so no LLVM struct type is needed for
        them in this pass.
        """
        self._structs["FileIO"] = [("handle", "ptr"), ("mode", "ptr"), ("closed", "i1")]
        self._struct_string_fields["FileIO"] = set()
        self._struct_field_nyet["FileIO"] = {"mode": "FileMode"}
        self._struct_type_lines.append("%FileIO = type { ptr, ptr, i1 }")
        self._method_impls[("FileIO", "write")] = "__builtin_fileio_write"
        self._method_impls[("FileIO", "read")] = "__builtin_fileio_read"
        self._method_impls[("FileIO", "close")] = "__builtin_fileio_close"

        # `_infer_nyet_type_name`'s generic method-call case AND
        # `_sum_name_of` (used by `match`/`?` to find a call's concrete
        # sum type) both need the callee registered in
        # `_fn_ret_nyet_names` — the former via the mangled sentinel
        # name, the latter via the *bare* call name only (no
        # receiver-type dispatch at all there), so both are registered.
        # Same flat-single-slot caveat as typeck's `self.env`
        # registration: a user channel whose own `read` returns a
        # *different* result type would collide here (whichever
        # registers last wins) — harmless in practice since every real
        # IOChannel implementer shares the trait's exact signature shape.
        for sentinel, bare, sum_name in (
            ("__builtin_fileio_write", "write", "WriteResult"),
            ("__builtin_fileio_read", "read", "ReadResult"),
            ("__builtin_fileio_close", "close", "CloseResult"),
        ):
            self._fn_ret_nyet_names[sentinel] = sum_name
            self._fn_ret_nyet_names[bare] = sum_name

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
            self._struct_type_lines.append(f"%{node.name} = type {{ i32 }}")
        else:
            self._struct_type_lines.append(f"%{node.name} = type {{ i32, [{max_payload} x i8] }}")

    def _register_const(self, node: N.ConstDecl) -> None:
        """Precompute a top-level `const`'s inlined value (see main.no's
        Constants section: consts are compile-time values with no runtime
        allocation or address). Only literal RHS values are supported,
        matching every documented/tested `const` use. A non-literal
        initializer raises rather than silently registering nothing --
        that silent-nothing behavior is exactly the original bug this
        method exists to fix (a dropped reference, not a clean error),
        and this compiler has no general constant-expression evaluator
        to honor a non-literal const value yet."""
        val = self._const_literal_value(node.value)
        if val is None:
            got = "no initializer" if node.value is None else type(node.value).__name__
            raise NotImplementedError(
                f"const '{node.name}': only literal initializers "
                f"(int/float/bool/string/keyword) are supported by this "
                f"compiler; got {got}"
            )
        llvm_val, default_ty = val
        llvm_ty = self._llvm_type(node.type) if node.type is not None else default_ty
        self._const_values[node.name] = (llvm_val, llvm_ty)

    def _const_literal_value(self, node: N.Expr | None) -> tuple[str, str] | None:
        """Return (llvm_immediate, llvm_type) for a literal expression, or
        None if it isn't one of the literal forms a `const` can hold."""
        if isinstance(node, N.IntLit):
            return str(node.value), self._infer_llvm_type(node)
        if isinstance(node, N.FloatLit):
            packed = _struct.pack("d", node.value)
            as_int = _struct.unpack("Q", packed)[0]
            return f"0x{as_int:016X}", "double"
        if isinstance(node, N.BoolLit):
            return ("1" if node.value else "0"), "i1"
        if isinstance(node, N.StringLit):
            name, _ = self._get_string(node.value)
            return name, "ptr"
        if isinstance(node, N.KeywordLit):
            return str(self._keyword_id(node.name)), "i32"
        return None

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
        if name == "char":
            return "i32"
        if name == "Keyword":
            return "i32"
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
            "i32": "i32",
            "i64": "i64",
            "double": "f64",
            "float": "f32",
            "i1": "bool",
            "i8": "i8",
            "i16": "i16",
        }
        if ty in mapping:
            return mapping[ty]
        if ty == "ptr":
            return "string"  # default ptr → string for mangling
        return "i32"

    def _subst_type(self, tn: N.TypeNode | None, env: dict[str, N.TypeNode]) -> N.TypeNode | None:
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
        # Compound types: `&T`, `#(A B)`, and `(fn T -> U)` used to come back
        # unsubstituted, so e.g. `swap[A B] (p:#(A B)) -> #(B A)` kept `A`/`B`
        # in its monomorphized signature.
        if isinstance(tn, N.RefType):
            return N.RefType(tn.span, self._subst_type(tn.inner, env) or tn.inner, tn.mutable)
        if isinstance(tn, N.TupleType):
            return N.TupleType(tn.span, [self._subst_type(e, env) or e for e in tn.elements])
        if isinstance(tn, N.FnType):
            return N.FnType(
                tn.span,
                [self._subst_type(p, env) or p for p in tn.params],
                self._subst_type(tn.ret, env),
            )
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
            if node.head is not None:
                n.head = self._subst_body(node.head, env)
            return n
        if isinstance(node, N.Path) and node.segments and node.segments[0] in env:
            # `(T/zero)` inside a generic body -> `(i32/zero)`.
            target = self._nyet_type_name_of_node(env[node.segments[0]])
            if target is None:
                return node
            n = _copy.copy(node)
            n.segments = [target, *node.segments[1:]]
            return n
        if isinstance(node, N.FnExpr):
            n = _copy.copy(node)
            n.params = []
            for p in node.params:
                np = _copy.copy(p)
                np.type = self._subst_type(p.type, env)
                n.params.append(np)
            n.return_type = self._subst_type(node.return_type, env)
            if node.body is not None:
                n.body = self._subst_body(node.body, env)
            return n
        if isinstance(node, (N.ArrayLit, N.TupleLit)):
            n = _copy.copy(node)
            n.elements = [self._subst_body(e, env) for e in node.elements]
            return n
        if isinstance(node, (N.KeywordArg, N.Try, N.Await, N.Spawn, N.Cast)):
            n = _copy.copy(node)
            if node.value is not None:
                n.value = self._subst_body(node.value, env)
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

    def _monomorphize_fn(self, name: str, type_args: tuple[str, ...]) -> str | None:
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
        for gp, targ in zip(tmpl.generics, type_args, strict=False):
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
            # Deep-copied so lifting below never mutates the shared template.
            clone.body = self._subst_body(_copy.deepcopy(tmpl.body), env)
            # Lift this instantiation's lambdas, now that their types are concrete.
            lifted: list[N.FnDecl] = []
            clone.body = self._lift_in(clone.body, lifted)
            for decl in lifted:
                self._register_fn_sig(decl)
            self._pending_closures.extend(lifted)

        # Emit the specialized function
        self._emit_fn(clone)
        return mangled

    def _monomorphize_sum_type(self, name: str, type_args: tuple[str, ...]) -> str | None:
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
        for gp, targ in zip(tmpl.generics, type_args, strict=False):
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

    def _monomorphize_struct(self, name: str, type_args: tuple[str, ...]) -> str | None:
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
        for gp, targ in zip(tmpl.generics, type_args, strict=False):
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

    @staticmethod
    def _alignof(ty: str) -> int:
        """Natural alignment in bytes of an LLVM type, mirroring _sizeof."""
        if ty in ("i1", "i8"):
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

    def _keyword_id(self, name: str) -> int:
        """Intern a keyword literal's name to a stable small integer ID."""
        if name not in self._keyword_ids:
            self._keyword_ids[name] = len(self._keyword_ids)
        return self._keyword_ids[name]

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

    def _get_fmt_i64(self) -> str:
        if self._fmt_i64 is None:
            self._fmt_i64 = self._get_format_string("%lld", "i64")
        return self._fmt_i64

    def _get_fmt_f64(self) -> str:
        if self._fmt_f64 is None:
            self._fmt_f64 = self._get_format_string("%g", "f64")
        return self._fmt_f64

    def _get_fmt_u32(self) -> str:
        if self._fmt_u32 is None:
            self._fmt_u32 = self._get_format_string("%u", "u32")
        return self._fmt_u32

    def _get_fmt_char(self) -> str:
        if self._fmt_char is None:
            self._fmt_char = self._get_format_string("%c", "char")
        return self._fmt_char

    def _get_fmt_u64(self) -> str:
        if self._fmt_u64 is None:
            self._fmt_u64 = self._get_format_string("%llu", "u64")
        return self._fmt_u64

    @staticmethod
    def _escape_bytes(data: bytes) -> str:
        result = []
        for b in data:
            if 32 <= b < 127 and b not in (ord('"'), ord("\\")):
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
            if name == "u32":
                return "i32"
            if name in ("u64", "usize"):
                # Falling through to the generic "i32" default below would
                # silently store a 64-bit-wide Nyet type in a 32-bit LLVM
                # int -- CLAUDE.md documents usize as "the indexing type",
                # so this would truncate any array-length-scale value.
                return "i64"
            if name == "char":
                return "i32"  # Unicode scalar value stored as i32
            if name == "Keyword":
                return "i32"  # interned symbol ID
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
            if base_name in ("Map", "Set"):
                # Hash table lowers to a heap pointer (see runtime/map.c).
                return "ptr"
            if base_name in ("Fn", "FnMut", "FnOnce"):
                return "ptr"  # a closure record -- see `_emit_closure_value`
            if base_name is not None:
                # Force inner generic args to monomorphize first so any
                # nested sum/struct types are registered before we use
                # their mangled names as a payload field type.
                for a in tn.args:
                    self._llvm_type(a)
                type_args = tuple(self._nyet_type_name_of_node(a) or "unk" for a in tn.args)
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
        if isinstance(tn, N.TupleType):
            # Heap-allocated, pass-by-pointer like structs.
            return "ptr"
        if isinstance(tn, N.DynType):
            # Fat pointer {data, vtable} -- see _get_or_register_dyn_type.
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

    def _array_of_array_elem_llvm_type(self, tn: N.TypeNode | None) -> str | None:
        """If `tn` is `Array[Array[U]]` (or `&Array[Array[U]]`), return
        U's LLVM element type -- i.e. one level deeper than
        `_array_elem_llvm_type`. See `_env_array_elem_of_array`."""
        if tn is None:
            return None
        if isinstance(tn, N.RefType):
            return self._array_of_array_elem_llvm_type(tn.inner)
        if isinstance(tn, N.GenericType):
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name == "Array" and tn.args:
                return self._array_elem_llvm_type(tn.args[0])
        return None

    def _array_elem_nyet_name(self, tn: N.TypeNode | None) -> str | None:
        """Return the Nyet element type name if `tn` is `Array[T]` (or
        `&Array[T]`) and `T` is a named type (e.g. a struct/sum type).
        Mirrors `_array_elem_llvm_type`, but keeps the *Nyet* name so a
        struct read out of an array can still be field-accessed -- see
        `_env_array_elem_nyet`."""
        if tn is None:
            return None
        if isinstance(tn, N.RefType):
            return self._array_elem_nyet_name(tn.inner)
        if isinstance(tn, N.GenericType):
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name == "Array" and tn.args:
                return self._nyet_type_name(tn.args[0])
        return None

    def _dyn_trait_name(self, tn: N.TypeNode | None) -> str | None:
        """Return the trait name if `tn` is `dyn Trait` (or `&dyn Trait`)."""
        if tn is None:
            return None
        if isinstance(tn, N.RefType):
            return self._dyn_trait_name(tn.inner)
        if isinstance(tn, N.DynType):
            trait = tn.trait
            if isinstance(trait, (N.NamedType, N.PrimType)):
                return trait.name
        return None

    def _map_val_llvm_type(self, tn: N.TypeNode | None) -> str | None:
        """Return the LLVM value type if `tn` is `Map[K V]` (or `&Map[K V]`)."""
        if tn is None:
            return None
        if isinstance(tn, N.RefType):
            return self._map_val_llvm_type(tn.inner)
        if isinstance(tn, N.GenericType):
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name == "Map" and len(tn.args) >= 2:
                return self._llvm_type(tn.args[1])
        return None

    def _map_val_nyet_name(self, tn: N.TypeNode | None) -> str | None:
        """Return the Nyet value type name if `tn` is `Map[K V]` (or
        `&Map[K V]`) and `V` is a named type. Mirrors
        `_array_elem_nyet_name` for the same reason — see
        `_env_map_val_nyet`."""
        if tn is None:
            return None
        if isinstance(tn, N.RefType):
            return self._map_val_nyet_name(tn.inner)
        if isinstance(tn, N.GenericType):
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name == "Map" and len(tn.args) >= 2:
                return self._nyet_type_name(tn.args[1])
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
            type_args = tuple(self._nyet_type_name_of_node(a) or "unk" for a in tn.args)
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
            "i32": "i32",
            "int": "i32",
            "i64": "i64",
            "f64": "double",
            "f32": "float",
            "bool": "i1",
            "string": "ptr",
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
            "env_char_names": set(self._env_char_names),
            "env_string_names": set(self._env_string_names),
            "env_unsigned_names": set(self._env_unsigned_names),
            "env_tuple_types": dict(self._env_tuple_types),
            "env_map_val_ty": dict(self._env_map_val_ty),
            "env_dyn_trait": dict(self._env_dyn_trait),
            "env_fn_sig": dict(self._env_fn_sig),
            "loop_stack": list(self._loop_stack),
            "str_lits": dict(self._str_lits),
            "current_fn_array_ret_elem_ty": self._current_fn_array_ret_elem_ty,
            "current_fn_return_type_node": self._current_fn_return_type_node,
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
        self._env_char_names = saved["env_char_names"]
        self._env_string_names = saved["env_string_names"]
        self._env_unsigned_names = saved["env_unsigned_names"]
        self._env_tuple_types = saved["env_tuple_types"]
        self._env_map_val_ty = saved["env_map_val_ty"]
        self._env_dyn_trait = saved["env_dyn_trait"]
        self._env_fn_sig = saved["env_fn_sig"]
        self._loop_stack = saved["loop_stack"]
        self._str_lits = saved["str_lits"]
        self._current_fn_array_ret_elem_ty = saved["current_fn_array_ret_elem_ty"]
        self._current_fn_return_type_node = saved["current_fn_return_type_node"]

    def _mut_ref_scalar_type(self, tn: N.TypeNode | None) -> str | None:
        """The LLVM type of T for a `&!T` param whose T is a scalar
        (int/float/bool/char), else None.

        Such a param is passed as a pointer to the caller's storage so
        writes through it are visible to the caller. `&!` of an aggregate
        (struct/sum/array/tuple/map/string) needs no change -- those values
        are already pointer-shaped. Before this, `&!i32` lowered to a plain
        by-value `i32`, so `(fn mutate (x:&!i32) -> unit (= x (+ x 1)))`
        silently mutated a private copy."""
        if not isinstance(tn, N.RefType) or not tn.mutable:
            return None
        if not isinstance(tn.inner, (N.PrimType, N.NamedType)):
            return None
        inner = self._llvm_type(tn.inner)
        return None if inner == "ptr" else inner

    def _param_llvm_type(self, tn: N.TypeNode | None) -> str:
        return "ptr" if self._mut_ref_scalar_type(tn) is not None else self._llvm_type(tn)

    def _record_mut_ref_scalar_params(self, node: N.FnDecl) -> None:
        kinds = [self._mut_ref_scalar_type(p.type) for p in node.params]
        if any(k is not None for k in kinds):
            self._fn_param_mut_ref[node.name] = kinds

    def _receiver_method(self, receiver: N.Expr, name: str) -> str | None:
        """The mangled impl method `name` on `receiver`'s type, if any."""
        probe = self._unwrap_borrow(receiver)
        type_name = self._infer_nyet_type_name(probe)
        if type_name is None and isinstance(probe, N.Ident):
            type_name = self._env_struct_name.get(probe.name)
        if type_name is None:
            return None
        return self._method_impls.get((type_name, name))

    def _record_nested_tuple_fields(self, tname: str, node: N.LetDecl | N.ConstDecl) -> None:
        """Remember which fields of tuple type `tname` hold tuples themselves
        (from a `#(...)` annotation or literal), so indexing one out and
        binding it keeps its tuple shape."""
        if isinstance(node.type, N.TupleType):
            for i, et in enumerate(node.type.elements):
                if isinstance(et, N.TupleType):
                    self._tuple_field_tuple[(tname, i)] = self._get_or_register_tuple_type(
                        [self._llvm_type(t) for t in et.elements],
                        [self._nyet_type_name(t) for t in et.elements],
                    )
        elif isinstance(node.value, N.TupleLit):
            for i, e in enumerate(node.value.elements):
                if isinstance(e, N.TupleLit):
                    self._tuple_field_tuple[(tname, i)] = self._get_or_register_tuple_type(
                        [self._infer_llvm_type(x) for x in e.elements],
                        [self._infer_nyet_type_name(x) for x in e.elements],
                    )

    def _call_operator_impl(self, head: N.Expr | None) -> str | None:
        """The mangled `()` impl for a local binding whose struct type
        implements the call operator (the Index trait), if any."""
        if not isinstance(head, N.Ident) or head.name not in self._env:
            return None
        type_name = self._env_struct_name.get(head.name)
        if type_name is None:
            return None
        return self._method_impls.get((type_name, "()"))

    def _record_array_return(self, node: N.FnDecl) -> None:
        """Record an `Array[T]` or function-typed return, so binding the call's
        result keeps it indexable / callable."""
        ret_elem = self._array_elem_llvm_type(node.return_type)
        if ret_elem is not None:
            self._fn_ret_array_elem[node.name] = ret_elem
        fn_ret = self._as_fn_type(node.return_type)
        if fn_ret is not None:
            self._fn_ret_fn_sig[node.name] = (
                [self._llvm_type(pt) for pt in fn_ret.params],
                self._llvm_ret_type(fn_ret.ret) if fn_ret.ret else "void",
            )

    def _emit_place_ptr(self, arg: N.Expr) -> str:
        """Address of the storage an `&!x` argument names, for passing to
        a `&!T` scalar parameter."""
        place = self._unwrap_borrow(arg)
        if isinstance(place, N.Ident) and place.name in self._env:
            return self._env[place.name][0]
        if isinstance(place, N.FieldAccess):
            ptr_and_ty = self._emit_field_ptr(place)
            if ptr_and_ty is not None:
                return ptr_and_ty[0]
        raise NotImplementedError(
            f"codegen: a `&!T` argument must name a variable or a struct field "
            f"(got {type(place).__name__}) -- a mutable borrow of a temporary or "
            f"an array element isn't supported yet"
        )

    def _register_fn_sig(self, node: N.FnDecl) -> None:
        """Pre-populate `_fn_sigs` so call sites resolve regardless of
        the order functions appear in the program list."""
        if node.name == "main":
            return
        param_types = [self._param_llvm_type(p.type) for p in node.params]
        self._record_mut_ref_scalar_params(node)
        self._record_array_return(node)
        ret_type = self._llvm_ret_type(node.return_type)
        self._fn_sigs[node.name] = (param_types, ret_type)
        ret_nyet = self._nyet_type_name(node.return_type) if node.return_type else None
        if ret_nyet:
            self._fn_ret_nyet_names[node.name] = ret_nyet
        if isinstance(node.return_type, N.TupleType):
            elem_tys = [self._llvm_type(et) for et in node.return_type.elements]
            elem_nyet = [self._nyet_type_name(et) for et in node.return_type.elements]
            self._fn_ret_tuple_types[node.name] = self._get_or_register_tuple_type(
                elem_tys, elem_nyet
            )
        dyn_traits = [self._dyn_trait_name(p.type) for p in node.params]
        if any(t is not None for t in dyn_traits):
            self._fn_param_dyn_traits[node.name] = dyn_traits
        array_elems = [self._array_elem_llvm_type(p.type) for p in node.params]
        if any(t is not None for t in array_elems):
            self._fn_param_array_elem[node.name] = array_elems

    def _body_definitely_returns(self, node: N.Node | None) -> bool:
        """True if executing `node` is guaranteed to already have emitted
        its own `ret` terminator -- a bare `(return expr)` (or a `do`
        block whose last statement is one) as a function's ENTIRE body
        yields no SSA value back to `_emit_fn` (by design -- see the
        `N.Return` case in `_emit_expr`), which `_emit_fn` used to treat
        exactly like a fallthrough with no explicit return, appending a
        second, invalid trailing `ret` right after the body's own one
        (`ret ptr %t6` followed by `ret ptr 0` -- two terminators in the
        same block with no label between them). This happened to still
        parse for every INTEGER return type (`ret i32 0` is syntactically
        valid on its own even as dead code LLVM's IR parser evidently
        doesn't reject), silently leaving genuinely malformed IR behind,
        but is a hard, unmissable clang build failure for any pointer
        return type (`ret ptr 0` -- "integer constant must have integer
        type") -- exactly the shape a `-> Array[T]`/struct/string-
        returning function with a bare top-level `(return ...)` takes.
        """
        if isinstance(node, N.Return):
            return True
        if isinstance(node, N.Do) and node.exprs:
            return self._body_definitely_returns(node.exprs[-1])
        return False

    def _emit_drops(self) -> None:
        """Free this function's dropped struct locals (see _drop_names'
        docstring) -- call only at the natural end-of-body fallthrough,
        never at an early return."""
        for name in self._drop_names.get(self._current_fn_name or "", []):
            if name not in self._env:
                continue
            ptr_slot, _ = self._env[name]
            val = self._fresh_tmp()
            self._emit_line(f"{val} = load ptr, ptr {ptr_slot}")
            self._declare_extern("declare void @free(ptr)")
            self._emit_line(f"call void @free(ptr {val})")

    def _emit_fn(self, node: N.FnDecl) -> None:
        saved = self._save_fn_state()
        self._tmp = 0
        self._label = 0
        self._fn_lines = []
        self._fn_alloca_lines = []
        self._current_fn_name = node.name
        self._current_fn_array_ret_elem_ty = None
        self._current_fn_return_type_node = node.return_type if node.name != "main" else None

        # Emit into a local buffer, then flush to self._lines at end.
        body_lines: list[str] = []

        if node.name == "main":
            body_lines.append("define i32 @main() {")
            self._emit_label("entry")
            if node.body is not None:
                self._emit_expr(node.body)
            self._emit_drops()
            self._emit_line("ret i32 0")
        else:
            param_types = []
            param_names = []
            param_nyet_names = []  # original Nyet type names for struct detection
            for p in node.params:
                param_types.append(self._param_llvm_type(p.type))
                param_names.append(p.name)
                param_nyet_names.append(self._nyet_type_name(p.type))
            self._record_mut_ref_scalar_params(node)
            self._record_array_return(node)

            ret_type = self._llvm_ret_type(node.return_type)
            self._fn_sigs[node.name] = (param_types, ret_type)
            ret_nyet = self._nyet_type_name(node.return_type) if node.return_type else None
            if ret_nyet:
                self._fn_ret_nyet_names[node.name] = ret_nyet
            # A declared `-> Array[T]` return type -- consulted below (and
            # by the `N.Return` case in `_emit_expr`) so `(array_new n)`
            # works as a function's tail-return value or an explicit
            # `(return (array_new n))`, not just a `let`'s direct RHS.
            self._current_fn_array_ret_elem_ty = self._array_elem_llvm_type(node.return_type)
            if isinstance(node.return_type, N.TupleType):
                elem_tys = [self._llvm_type(et) for et in node.return_type.elements]
                elem_nyet = [self._nyet_type_name(et) for et in node.return_type.elements]
                self._fn_ret_tuple_types[node.name] = self._get_or_register_tuple_type(
                    elem_tys, elem_nyet
                )

            params_str = ", ".join(
                f"{t} %{n}" for t, n in zip(param_types, param_names, strict=False)
            )
            if node.name in self._closure_info:
                # A lifted lambda takes its closure record as a hidden first
                # parameter -- see `_emit_closure_value`.
                params_str = "ptr %__env" + (", " + params_str if params_str else "")
            body_lines.append(f"define {ret_type} @{node.name}({params_str}) {{")
            self._emit_label("entry")
            if node.name in self._closure_info:
                # Before params, so a param shadows a same-named capture.
                self._bind_closure_captures(node.name)

            for p, t, n, nyet_n in zip(
                node.params, param_types, param_names, param_nyet_names, strict=False
            ):
                arr_elem = self._array_elem_llvm_type(p.type)
                if arr_elem is not None:
                    # Array params arrive as `ptr` to the heap block.
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    self._env_array_elem[n] = arr_elem
                    arr_elem_nyet = self._array_elem_nyet_name(p.type)
                    if arr_elem_nyet is not None:
                        self._env_array_elem_nyet[n] = arr_elem_nyet
                elif self._as_fn_type(p.type) is not None:
                    # Function-typed param: a pointer to a function. Track
                    # its signature so calls like `(f x)` can be lowered as
                    # an indirect call through the slot. Unwrap `&`/`&!`
                    # first -- same fix as the tuple-param case just above,
                    # same bug: a by-reference fn-typed param (`f:&(fn i32
                    # -> i32)`) fell into the generic scalar-`ptr` fallback
                    # below and `(f x)` was parsed as a call to an
                    # undefined function `f`.
                    fn_type = self._as_fn_type(p.type)
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    fn_param_tys = [self._llvm_type(pt) for pt in fn_type.params]
                    fn_ret_ty = self._llvm_ret_type(fn_type.ret) if fn_type.ret else "void"
                    self._env_fn_sig[n] = (fn_param_tys, fn_ret_ty)
                elif nyet_n and (nyet_n in self._structs or nyet_n in self._sum_types):
                    # Struct/sum params are already ptrs — register directly
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    self._env_struct_name[n] = nyet_n
                elif isinstance(
                    p.type.inner if isinstance(p.type, N.RefType) else p.type, N.TupleType
                ):
                    # Tuple params are already ptrs — register their shape
                    # so `(param i)` lowers to indexed field load. Unwrap
                    # `&`/`&!` first (unlike the array/struct/sum/dyn-Trait
                    # cases above, which already unwrap internally via
                    # `_array_elem_llvm_type`/`_nyet_type_name`/
                    # `_dyn_trait_name`) -- without it, a by-reference tuple
                    # param (`t:&#(T1 T2)`, the natural way to avoid copying
                    # one into a function) fell into the generic scalar-`ptr`
                    # fallback below and `(t i)` was parsed as a call to an
                    # undefined function `t`, the same bug already fixed for
                    # Map[K V] params.
                    tuple_type = p.type.inner if isinstance(p.type, N.RefType) else p.type
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    tup_elem_tys = [self._llvm_type(et) for et in tuple_type.elements]
                    tup_elem_nyet = [self._nyet_type_name(et) for et in tuple_type.elements]
                    self._env_tuple_types[n] = self._get_or_register_tuple_type(
                        tup_elem_tys, tup_elem_nyet
                    )
                elif self._map_val_llvm_type(p.type) is not None:
                    # Map[K V] params arrive as `ptr` to the runtime hash
                    # table, exactly like a local `let`/`var` — without
                    # this case they fell into the generic `else` branch
                    # below (an opaque scalar `ptr` param with no lookup
                    # dispatch at all), so `(m key)` inside the function
                    # body was misparsed as a call to an undefined
                    # function literally named `m` instead of a map
                    # lookup.
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    self._env_map_val_ty[n] = self._map_val_llvm_type(p.type)
                    map_val_nyet = self._map_val_nyet_name(p.type)
                    if map_val_nyet is not None:
                        self._env_map_val_nyet[n] = map_val_nyet
                elif self._dyn_trait_name(p.type) is not None:
                    # `dyn Trait` params arrive as an already-constructed
                    # fat pointer (the caller coerces -- see
                    # _emit_user_call). Register the trait so calls like
                    # `(method param)` inside this body dispatch via
                    # vtable instead of static lookup.
                    ptr = self._emit_alloca("ptr")
                    self._emit_line(f"store ptr %{n}, ptr {ptr}")
                    self._env[n] = (ptr, "ptr")
                    self._env_dyn_trait[n] = self._dyn_trait_name(p.type)
                elif (mut_ref_ty := self._mut_ref_scalar_type(p.type)) is not None:
                    # `&!T` scalar: the incoming pointer IS the binding's
                    # storage, so reads load through it and `(= x v)` stores
                    # through it -- see `_mut_ref_scalar_type`.
                    self._env[n] = (f"%{n}", mut_ref_ty)
                else:
                    ptr = self._emit_alloca(t)
                    self._emit_line(f"store {t} %{n}, ptr {ptr}")
                    self._env[n] = (ptr, t)
                # Track char-typed params for _emit_cast.
                if p.type is not None and self._nyet_type_name(p.type) == "char":
                    self._env_char_names.add(n)
                # Track string-typed params so `(param i)` lowers to a byte
                # index. Array/struct/fn params were handled above and never
                # reach here, so this only catches genuine `string` scalars.
                if p.type is not None and self._nyet_type_name(p.type) == "string":
                    self._env_string_names.add(n)
                # Track unsigned-typed params -- see `_env_unsigned_names`.
                if p.type is not None and self._nyet_type_name(p.type) in self._UNSIGNED_NAMES:
                    self._env_unsigned_names.add(n)

            if node.body is not None:
                if self._current_fn_array_ret_elem_ty is not None:
                    result = self._emit_expr_as_array(node.body, self._current_fn_array_ret_elem_ty)
                else:
                    result = self._emit_expr(node.body)
                if self._body_definitely_returns(node.body):
                    # The body's own `(return ...)` already emitted this
                    # block's terminator -- see `_body_definitely_returns`.
                    pass
                else:
                    self._emit_drops()
                    if ret_type == "void":
                        self._emit_line("ret void")
                    elif result is not None:
                        self._emit_line(f"ret {ret_type} {result}")
                    else:
                        self._emit_line(f"ret {ret_type} {self._zero_value(ret_type)}")
            else:
                self._emit_drops()
                if ret_type == "void":
                    self._emit_line("ret void")
                else:
                    self._emit_line(f"ret {ret_type} {self._zero_value(ret_type)}")

        # Splice this fn's body into body_lines, then append all at once
        if self._fn_lines:
            body_lines.append(self._fn_lines[0])  # "entry:"
            body_lines.extend(self._fn_alloca_lines)
            body_lines.extend(self._fn_lines[1:])
        body_lines.append("}")
        body_lines.append("")

        self._restore_fn_state(saved)
        self._lines.extend(body_lines)

    def _emit_closures(self) -> None:
        """Emit lifted lambdas (`_pending_closures`) after the code that
        creates them.

        A capturing lambda's body needs the types of its captures, which are
        snapshotted where the closure is created (`_emit_closure_value`); a
        lambda nested in another is only created while the outer lambda's body
        is emitted; and monomorphizing a generic function lifts new lambdas --
        so emit in rounds until nothing is ready. A capturing lambda that is
        never created anywhere is unreachable, and is skipped."""
        while True:
            pending = self._pending_closures
            ready = [
                fn
                for fn in pending
                if not self._closure_info[fn.name][0] or fn.name in self._closure_capture_meta
            ]
            if not ready:
                return
            ready_ids = {id(fn) for fn in ready}
            self._pending_closures = [fn for fn in pending if id(fn) not in ready_ids]
            for fn in ready:
                self._emit_fn(fn)

    # Per-binding side tables copied into a lambda for each captured name.
    _BINDING_META_DICTS = (
        "_env_struct_name",
        "_env_array_elem",
        "_env_array_elem_nyet",
        "_env_array_elem_fn_sig",
        "_env_array_elem_of_array",
        "_env_tuple_types",
        "_env_map_val_ty",
        "_env_map_val_nyet",
        "_env_map_val_fn_sig",
        "_env_dyn_trait",
        "_env_fn_sig",
        "_str_lits",
    )
    _BINDING_META_SETS = ("_env_char_names", "_env_string_names", "_env_unsigned_names")

    def _snapshot_binding_meta(self, name: str) -> dict:
        meta: dict = {}
        for attr in self._BINDING_META_DICTS:
            table = getattr(self, attr)
            if name in table:
                meta[attr] = table[name]
        for attr in self._BINDING_META_SETS:
            if name in getattr(self, attr):
                meta[attr] = True
        return meta

    def _restore_binding_meta(self, name: str, meta: dict) -> None:
        for attr, value in meta.items():
            if attr in self._BINDING_META_SETS:
                getattr(self, attr).add(name)
            else:
                getattr(self, attr)[name] = value

    def _emit_closure_value(self, name: str) -> str:
        """The value of lifted lambda `name`, built where the lambda appears.

        Every function value is a pointer to a closure record whose first
        field is the code pointer; calls pass the record itself as a hidden
        first argument (`_emit_indirect_call_raw`). A lambda with no captures
        uses a static record. Otherwise the record is heap-allocated with one
        field per capture: its value for `move fn`, or the address of the
        enclosing binding for `fn` / `fn!`, so a borrowing closure sees (and
        a `fn!` closure makes) later changes to it. A borrowing closure must
        not outlive the function that created it; that isn't checked yet."""
        captures, mode = self._closure_info[name]
        if not captures:
            return self._static_closure_record(name)
        by_ref = mode is not N.CaptureMode.MOVE
        field_tys = ["ptr"]
        metas: list[tuple[str, str, bool, dict]] = []
        for cname in captures:
            if cname not in self._env:
                raise NotImplementedError(
                    f"codegen: closure {name} captures '{cname}', which isn't a local "
                    f"variable where the closure is created"
                )
            _, ty = self._env[cname]
            field_tys.append("ptr" if by_ref else ty)
            metas.append((cname, ty, by_ref, self._snapshot_binding_meta(cname)))
        layout = "{ " + ", ".join(field_tys) + " }"
        self._closure_layout.setdefault(name, layout)
        self._closure_capture_meta.setdefault(name, metas)

        self._declare_extern("declare ptr @malloc(i64)")
        record = self._fresh_tmp()
        self._emit_line(f"{record} = call ptr @malloc(i64 {8 * len(field_tys)})")
        self._emit_line(f"store ptr @{name}, ptr {record}")
        for k, cname in enumerate(captures):
            slot, ty = self._env[cname]
            field_ptr = self._fresh_tmp()
            self._emit_line(
                f"{field_ptr} = getelementptr inbounds {layout}, ptr {record}, i32 0, i32 {k + 1}"
            )
            if by_ref:
                self._emit_line(f"store ptr {slot}, ptr {field_ptr}")
            else:
                val = self._fresh_tmp()
                self._emit_line(f"{val} = load {ty}, ptr {slot}")
                self._emit_line(f"store {ty} {val}, ptr {field_ptr}")
        return record

    def _bind_closure_captures(self, name: str) -> None:
        """Inside lifted lambda `name`, bind each captured name from `%__env`."""
        layout = self._closure_layout.get(name)
        for k, (cname, ty, by_ref, meta) in enumerate(self._closure_capture_meta.get(name, [])):
            field_ptr = self._fresh_tmp()
            self._emit_line(
                f"{field_ptr} = getelementptr inbounds {layout}, ptr %__env, i32 0, i32 {k + 1}"
            )
            if by_ref:
                slot = self._fresh_tmp()
                self._emit_line(f"{slot} = load ptr, ptr {field_ptr}")
            else:
                slot = field_ptr
            self._env[cname] = (slot, ty)
            self._restore_binding_meta(cname, meta)

    def _static_closure_record(self, fn_symbol: str) -> str:
        """A constant closure record `{ ptr @fn_symbol }` for a function value
        with no captures. `fn_symbol` must take the hidden record parameter."""
        key = f"{fn_symbol}.closure"
        if key not in self._synth_fns:
            self._synth_fns[key] = f"@{key} = internal constant {{ ptr }} {{ ptr @{fn_symbol} }}\n"
        return f"@{key}"

    def _fn_value_record(self, name: str) -> str:
        """The closure value for named function `name` used as a value: a
        thunk that takes (and ignores) the hidden record parameter and
        forwards to the function, in a static record."""
        thunk = f"{name}.thunk"
        if thunk not in self._synth_fns:
            param_tys, ret_ty = self._fn_sigs[name]
            params = ", ".join(["ptr %__env"] + [f"{t} %a{i}" for i, t in enumerate(param_tys)])
            args = ", ".join(f"{t} %a{i}" for i, t in enumerate(param_tys))
            if ret_ty == "void":
                body = [f"  call void @{name}({args})", "  ret void"]
            else:
                body = [f"  %r = call {ret_ty} @{name}({args})", f"  ret {ret_ty} %r"]
            self._synth_fns[thunk] = "\n".join(
                [f"define internal {ret_ty} @{thunk}({params}) {{", "entry:", *body, "}", ""]
            )
        return self._static_closure_record(thunk)

    def _as_fn_type(self, tn: N.TypeNode | None) -> N.FnType | None:
        """`(fn A -> R)`, `&(fn ...)`, or `Fn[(A) R]` / `FnMut[...]` / `FnOnce[...]`
        as an `FnType`; None for any other type."""
        if isinstance(tn, N.RefType):
            tn = tn.inner
        if isinstance(tn, N.FnType):
            return tn
        if (
            isinstance(tn, N.GenericType)
            and isinstance(tn.base, (N.NamedType, N.PrimType))
            and tn.base.name in ("Fn", "FnMut", "FnOnce")
        ):
            params_node = tn.args[0] if tn.args else None
            if params_node is None or isinstance(params_node, N.UnitType):
                params: list[N.TypeNode] = []
            elif isinstance(params_node, N.TupleType):
                params = list(params_node.elements)
            else:
                params = [params_node]
            return N.FnType(tn.span, params, tn.args[1] if len(tn.args) > 1 else None)
        return None

    @staticmethod
    def _float_const(value: float, ty: str) -> str:
        """An LLVM hex float constant for `value` as `ty` ("float" or "double")."""
        if ty == "float":
            value = _struct.unpack("f", _struct.pack("f", value))[0]
        return f"0x{_struct.unpack('Q', _struct.pack('d', value))[0]:016X}"

    def _zero_value(self, ty: str) -> str:
        """A literal zero of LLVM type `ty` -- for a body that yields no value
        (e.g. a `...` placeholder). `ret ptr 0` is invalid IR."""
        if ty == "ptr":
            return "null"
        if self._is_float(ty):
            return "0.0"
        return "0"

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

    def _find_do_tail_let(self, node: N.Do) -> N.LetDecl | None:
        """If `node`'s tail expression is a bare reference to a name
        bound by an earlier `let`/`var` in that SAME `do` block, return
        that declaration.

        `_infer_llvm_type`/`_infer_nyet_type_name` run before the do's
        contents are actually emitted (they size an `alloca`/determine a
        Nyet type ahead of emission), so `self._env` does not have the
        binding yet — a plain `self._env[name]` lookup inside those
        functions' `N.Ident` case silently misses it and falls through
        to the wrong default (`i32` / `None`). E.g. `(let total (do
        (let r (returns_i64)) (out "...") r))` used to size `total` as
        i32, silently truncating a real i64 result. Scans forward and
        keeps the LAST match so shadowing (`(let r ...) ... (let r
        ...))` resolves to the binding actually in scope at the tail.
        """
        if not node.exprs:
            return None
        tail = node.exprs[-1]
        if not isinstance(tail, N.Ident):
            return None
        found: N.LetDecl | None = None
        for prior in node.exprs[:-1]:
            if isinstance(prior, N.LetDecl) and prior.name == tail.name:
                found = prior
        return found

    def _infer_llvm_type(self, node: N.Node | None) -> str:
        if isinstance(node, N.IntLit):
            # The parser drops any `i64` suffix, so magnitude is the only
            # signal left: a literal outside i32's range must have been
            # written as (or promoted to) i64. Getting this wrong used to
            # make `_emit_let`/`_emit_arith` coerce via `sext i32 <value>
            # to i64`, which parses the literal as a 32-bit constant FIRST
            # (silently wrapping it) and only then extends the wrapped
            # result -- a silent-truncation miscompile for any large i64
            # literal, not just a missing-width cosmetic issue.
            if node.value > 0x7FFFFFFF or node.value < -0x80000000:
                return "i64"
            return "i32"
        if isinstance(node, N.FloatLit):
            return "double"
        if isinstance(node, N.BoolLit):
            return "i1"
        if isinstance(node, N.StringLit):
            return "ptr"
        if isinstance(node, N.KeywordLit):
            return "i32"
        if isinstance(node, N.ArrayLit):
            return "ptr"
        if isinstance(node, N.TupleLit):
            return "ptr"
        if isinstance(node, N.MapLit):
            return "ptr"
        if isinstance(node, N.Cast):
            return self._llvm_type(node.target_type)
        if isinstance(node, N.Ident) and node.name in self._env:
            return self._env[node.name][1]
        if isinstance(node, N.Ident) and node.name in self._const_values:
            return self._const_values[node.name][1]
        # v0.6: a bare reference to a top-level fn yields its function pointer.
        if isinstance(node, N.Ident) and node.name in self._fn_sigs:
            return "ptr"
        if isinstance(node, N.Ident) and (
            node.name in self._variant_ctors or node.name in self._generic_variant_ctors
        ):
            return "ptr"  # a bare nullary variant value -- see `_emit_ident`
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            op = node.head.name
            call_impl = self._call_operator_impl(node.head)
            if call_impl is not None and call_impl in self._fn_sigs:
                return self._fn_sigs[call_impl][1]
            # Array indexing: shadows any same-named function.
            if op in self._env_array_elem and len(node.args) == 1:
                return self._env_array_elem[op]
            # String indexing yields a char, stored as i32.
            if op in self._env_string_names and len(node.args) == 1:
                return "i32"
            # Tuple indexing: `(t 0)` yields the element type at that
            # position (only known for a literal integer index).
            if (
                op in self._env_tuple_types
                and len(node.args) == 1
                and isinstance(node.args[0], N.IntLit)
            ):
                fields = self._structs[self._env_tuple_types[op]]
                idx = node.args[0].value
                if 0 <= idx < len(fields):
                    return fields[idx][1]
            # Map lookup: `(m key)` yields the map's value type.
            if op in self._env_map_val_ty and len(node.args) == 1:
                return self._env_map_val_ty[op]
            if op in ("+", "-", "*", "/", "%"):
                if node.args:
                    sn = self._infer_nyet_type_name(node.args[0])
                    if sn is None and isinstance(node.args[0], N.Ident):
                        sn = self._env_struct_name.get(node.args[0].name)
                    if sn is not None and (sn, op) in self._method_impls:
                        mangled = self._method_impls[(sn, op)]
                        if mangled in self._fn_sigs:
                            return self._fn_sigs[mangled][1]
                if (
                    op in ("+", "/")
                    and node.args
                    and all(self._infer_llvm_type(a) == "ptr" for a in node.args)
                ):
                    # String concat / path join (see `_emit_arith`).
                    return "ptr"
                has_double = False
                has_float = False
                has_i64 = False
                for a in node.args:
                    t = self._infer_llvm_type(a)
                    if t == "double":
                        has_double = True
                    elif t == "float":
                        has_float = True
                    elif t == "i64":
                        has_i64 = True
                if has_double:
                    return "double"
                if has_float:
                    return "float"
                if has_i64:
                    return "i64"
                return "i32"
            if op in ("==", "!=", "<", ">", "<=", ">=", "&&", "||", "!"):
                return "i1"
            # HOF builtins over Array[T] (see _emit_call's dispatch table
            # a few hundred lines down) -- these aren't in `_fn_sigs`
            # since they're handled specially rather than as real
            # top-level functions, so without an explicit case here they
            # fell through every branch below to the final "i32" default.
            # That's silently wrong for map/filter/zip (all return a heap
            # Array[T], i.e. `ptr`) whenever the call is used inline
            # (e.g. `(print_array (map f arr))`) rather than through a
            # `let` with an explicit type annotation, which has its own
            # separate, correct type-driven path.
            if op in ("map", "filter", "flat_map", "zip"):
                return "ptr"
            if op in ("any", "all"):
                return "i1"
            if op == "fold" and node.args and isinstance(node.args[0], N.Ident):
                fname = node.args[0].name
                if fname in self._fn_sigs:
                    return self._fn_sigs[fname][1]
                if fname in self._env_fn_sig:
                    return self._env_fn_sig[fname][1]
                if fname in self._OP_CALLABLES and len(node.args) == 3:
                    fold_elem = self._array_expr_elem_ty(node.args[2])
                    if fold_elem is not None:
                        return fold_elem
            if op in ("min", "max") and len(node.args) == 2 and op not in self._fn_sigs:
                return self._min_max_type(node.args)
            if op in ("&", "&!") and len(node.args) == 1:
                return self._infer_llvm_type(node.args[0])
            if op == "fmt":
                return "ptr"
            if op == "now":
                return "double"
            if op == "sqrt" and len(node.args) == 1 and op not in self._fn_sigs:
                return "double"
            if op == "parse" and len(node.args) == 1 and op not in self._fn_sigs:
                return "ptr"  # a Result -- see `_emit_parse`
            if op in ("file_open", "file_read_all"):
                return "ptr"
            if op == "in":
                if (
                    node.args
                    and isinstance(node.args[0], N.Ident)
                    and node.args[0].name in self._IN_TYPE_NAMES
                ):
                    return self._llvm_type_from_name(node.args[0].name)
                # `(in)` bare or `(in channel)` explicit-channel read —
                # both yield a `string` (heap ptr), never a primitive.
                return "ptr"
            if op == "io" and len(node.args) == 1:
                return "ptr"
            if op in self._structs or op in self._variant_ctors:
                return "ptr"
            if op in self._fn_sigs:
                return self._fn_sigs[op][1]
            # dyn trait object dispatch — same method-return-type lookup
            # as the static path below, but via the trait declaration
            # since there's no single concrete struct name to key on.
            if node.args:
                probe = self._unwrap_borrow(node.args[0])
                if isinstance(probe, N.Ident) and probe.name in self._env_dyn_trait:
                    trait_name = self._env_dyn_trait[probe.name]
                    _, ret_ty = self._dyn_method_llvm_sig(trait_name, op)
                    return ret_ty
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
                    for gp, targ in zip(tmpl.generics, type_args, strict=False):
                        env[gp.name] = N.NamedType(tmpl.span, targ)
                    rt = self._subst_type(tmpl.return_type, env)
                    return self._llvm_type(rt)
            # v0.3: generic struct / generic variant
            if op in self._struct_templates:
                return "ptr"
            if op in self._generic_variant_ctors:
                return "ptr"
        if (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Path)
            and len(node.head.segments) == 2
        ):
            # `(Type/name ...)` — see `_emit_call`'s matching case.
            mangled = self._method_impls.get(tuple(node.head.segments))
            if mangled is not None and mangled in self._fn_sigs:
                return self._fn_sigs[mangled][1]
        if isinstance(node, N.Call) and (unq := self._unqualify_module_call(node)) is not None:
            return self._infer_llvm_type(unq)
        if isinstance(node, N.FieldAccess):
            return self._infer_field_type(node)
        if isinstance(node, N.If) and node.then_branch:
            return self._infer_llvm_type(node.then_branch)
        if isinstance(node, N.Match) and node.arms:
            # A match yields the type of its arm bodies (all arms agree);
            # mirror `_emit_match`, which sizes its result slot the same way.
            return self._infer_llvm_type(node.arms[0].body)
        if isinstance(node, N.Do) and node.exprs:
            prior_let = self._find_do_tail_let(node)
            if prior_let is not None:
                if prior_let.type is not None:
                    return self._llvm_type(prior_let.type)
                if prior_let.value is not None:
                    return self._infer_llvm_type(prior_let.value)
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
        if isinstance(node, N.Spawn):
            return "ptr"  # opaque Handle, see _emit_spawn
        if isinstance(node, N.Await):
            return "ptr"  # scoped to ptr-returning spawned calls
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

    def _struct_name_of(self, node: N.Node | None) -> str | None:
        """Try to determine which Nyet struct a node refers to.

        A thin `Node | None` wrapper around `_infer_nyet_type_name`,
        which handles every shape this needs (a plain binding, array/
        map-index reads, chained field access, if/match/do, direct
        struct/variant construction, static/inherent method calls,
        ...) and is kept as the single source of truth for "what Nyet
        type does this expression have" rather than duplicating that
        shape-by-shape here — a previous version of this method only
        handled a subset of those shapes (missing, at various points,
        direct constructor calls and if/match/do), which silently
        broke field access chained directly onto any of them (e.g.
        `(. (Point x:1 y:2) x)`, `(. (if cond a b) x)`).
        """
        if node is None:
            return None
        return self._infer_nyet_type_name(node)

    # ==================================================================
    # Expression emission
    # ==================================================================

    def _emit_expr(self, node: N.Node | None) -> str | None:
        if node is None:
            return None

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

        if isinstance(node, N.KeywordLit):
            return str(self._keyword_id(node.name))

        if isinstance(node, N.UnitLit):
            return None

        if isinstance(node, N.Todo):
            # A bare `...` placeholder: panic if it's ever reached.
            return self._emit_panic([N.StringLit(node.span, "not yet implemented")])

        if isinstance(node, N.Pass):
            return None

        if isinstance(node, N.Ident):
            return self._emit_ident(node)

        if isinstance(node, N.ArrayLit):
            return self._emit_array_lit(node)

        if isinstance(node, N.TupleLit):
            return self._emit_tuple_lit(node)

        if isinstance(node, N.MapLit):
            return self._emit_map_lit(node)

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

        if isinstance(node, N.Spawn):
            return self._emit_spawn(node)

        if isinstance(node, N.Await):
            return self._emit_await(node)

        if isinstance(node, N.Cast):
            return self._emit_cast(node.value, node.target_type)

        if isinstance(node, N.Do):
            return self._emit_do(node)

        if isinstance(node, (N.LetDecl, N.ConstDecl)):
            return self._emit_let(node)

        if isinstance(node, N.Return):
            # `(return ())` — an explicit unit value, e.g. from a macro
            # that returns early with a caller-supplied "what to return"
            # argument shared across `-> bool` and `-> unit` call sites —
            # has a real `node.value` (a UnitLit, not Python None) but
            # `_emit_expr` legitimately yields no SSA value for it (unit
            # has no runtime representation). Falling into the branch
            # below used to stringify that as the literal text "ret i32
            # None" (Python's `None` interpolated straight into the IR).
            # Both `(return)` and `(return ())` mean the same thing in a
            # `-> unit` function, so treat them the same.
            if node.value is not None and not isinstance(node.value, N.UnitLit):
                if self._current_fn_array_ret_elem_ty is not None:
                    # `(return (array_new n))` -- see `_emit_expr_as_array`.
                    val = self._emit_expr_as_array(node.value, self._current_fn_array_ret_elem_ty)
                    ty = "ptr"
                else:
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

        raise NotImplementedError(
            f"codegen: no _emit_expr case for {type(node).__name__} "
            f"(span {getattr(node, 'span', None)}) — was silently returning "
            f"None (and therefore emitting nothing) before this was flipped "
            f"to fail loudly; see CONTINUATION_PLAN.md"
        )

    def _emit_ident(self, node: N.Ident) -> str | None:
        if node.name in self._env:
            ptr, ty = self._env[node.name]
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = load {ty}, ptr {ptr}")
            return tmp
        # Top-level `const` — inlined immediate, no load (no address).
        if node.name in self._const_values:
            return self._const_values[node.name][0]
        # A lifted lambda or a named function used as a value yields a
        # closure record -- see `_emit_closure_value`.
        if node.name in self._closure_info:
            return self._emit_closure_value(node.name)
        if node.name in self._fn_sigs:
            return self._fn_value_record(node.name)
        # A bare nullary variant used as a value, e.g. `(Err DivisionByZero)`:
        # the same as calling its constructor with no arguments.
        if node.name in self._variant_ctors or node.name in self._generic_variant_ctors:
            return self._emit_call(N.Call(node.span, node, []))
        raise NotImplementedError(
            f"undefined identifier '{node.name}' at codegen time -- most likely a "
            f"closure referencing a variable from its enclosing scope: "
            f"`_lift_closures` hoists a `(fn ...)`/`(fn! ...)` literal to a "
            f"top-level function with no access to the enclosing scope's locals "
            f"('{node.name}' resolves fine at the sema level, where lexical "
            f"scoping still applies, but not after lifting) -- variable capture "
            f"in closures is not implemented yet (see CONTINUATION_PLAN.md)"
        )

    # ==================================================================
    # Calls — builtins, operators, struct ctors, user fns
    # ==================================================================

    def _unqualify_module_call(self, node: N.Call) -> N.Call | None:
        """`(std/math/sqrt 2.0)` / `(nyet/geometry/area s)` -> the same call
        through its plain name.

        The driver merges every loaded module into one flat program
        (`_load_program_with_deps`), so a module qualifier only documents
        where a name comes from. Only lowercase-rooted paths count as module
        paths; `(Type/name ...)` impl calls are handled separately. Before
        this, a module-qualified call evaluated to nothing at all."""
        head = node.head
        if not isinstance(head, N.Path) or len(head.segments) < 2:
            return None
        if not head.segments[0][:1].islower():
            return None
        return N.Call(node.span, N.Ident(head.span, head.segments[-1]), node.args)

    def _emit_call(self, node: N.Call) -> str | None:
        if isinstance(node.head, N.Ident):
            name = node.head.name
            # A value whose type implements the call operator `()` -- the Index
            # trait's `(fn () (self:&T idx:I) -> &O)` -- is callable: `(palette 1)`.
            call_impl = self._call_operator_impl(node.head)
            if call_impl is not None:
                return self._emit_user_call(call_impl, [node.head, *node.args])
            # Array indexing: `(arr i)` where `arr` is a local Array[T].
            # A bound name shadows any same-named function, matching scoping.
            if name in self._env_array_elem and len(node.args) == 1:
                return self._emit_array_index(name, node.args[0])
            # String indexing: `(str i)` where `str` is a local `string`.
            # Yields the byte at position `i` as a `char` (i32). Checked
            # after arrays so an Array binding always wins, matching scoping.
            if name in self._env_string_names and len(node.args) == 1:
                return self._emit_string_index(name, node.args[0])
            # Tuple indexing: `(t i)` where `t` is a local tuple binding.
            if name in self._env_tuple_types and len(node.args) == 1:
                return self._emit_tuple_index(name, node.args[0])
            # Map lookup: `(m key)` where `m` is a local Map[string V].
            if name in self._env_map_val_ty and len(node.args) == 1:
                return self._emit_map_get(name, node.args[0])
            # A method on the receiver shadows a builtin of the same name --
            # `(len &v)` on a `Vec2` with its own inherent `len`. Without this
            # the builtin array `len` ran on the struct pointer.
            # (Operators are excluded: they dispatch to impls in _emit_arith /
            # _emit_cmp, which also handle the variadic forms.)
            if node.args and name.isidentifier() and name not in self._fn_sigs:
                receiver_method = self._receiver_method(node.args[0], name)
                # `__builtin_*` targets are the sentinels
                # `_register_builtin_channel_structs` registers so the builtin
                # channels answer to inherent-method dispatch too. They have no
                # real emitted function behind them, so they must fall through
                # to the `write`/`read`/`close` builtin cases below, which emit
                # the operation inline.
                if receiver_method is not None and not receiver_method.startswith("__builtin_"):
                    return self._emit_user_call(receiver_method, node.args)
            # Builtins
            # `out` is deliberately absent here: the `out!`/`err!` prelude
            # macros expand to `(io out ...)`/`(io err ...)` before codegen
            # ever runs, so bare `out` is never a call head at this point.
            if name == "io":
                return self._emit_io(node.args)
            if name == "in":
                return self._emit_in(node.args)
            if name == "fmt":
                return self._emit_fmt(node.args)
            if name == "panic":
                return self._emit_panic(node.args)
            if name == "now" and len(node.args) == 0:
                return self._emit_now()
            if name == "sqrt" and len(node.args) == 1 and name not in self._fn_sigs:
                return self._emit_sqrt(node.args[0])
            if name == "parse" and len(node.args) == 1 and name not in self._fn_sigs:
                return self._emit_parse(node.args[0])
            if name == "len" and len(node.args) == 1:
                return self._emit_array_len(node.args[0])
            if name == "FileIO":
                return self._emit_fileio_open(node.args)
            # write/read/close on a FileIO receiver are hand-rolled
            # builtins (see `_register_builtin_channel_structs`), checked
            # by receiver type ahead of the generic inherent-method-
            # dispatch path below (which handles the same names for any
            # user-defined IOChannel implementer via real `_method_impls`
            # entries backed by an actual mangled function).
            if name in ("write", "read", "close") and node.args:
                probe = self._unwrap_borrow(node.args[0])
                sn = self._infer_nyet_type_name(probe)
                if sn is None and isinstance(probe, N.Ident):
                    sn = self._env_struct_name.get(probe.name)
                if sn == "FileIO":
                    if name == "write":
                        return self._emit_fileio_write(node.args)
                    if name == "read":
                        return self._emit_fileio_read(node.args)
                    return self._emit_fileio_close(node.args)
            if name == "file_open":
                return self._emit_file_open(node.args)
            if name == "file_read_all":
                return self._emit_file_read_all(node.args)
            if name == "file_read_lines":
                return self._emit_file_read_lines(node.args)
            if name == "file_write":
                return self._emit_file_write(node.args)
            if name == "file_close":
                return self._emit_file_close(node.args)
            # Higher-order functions over Array[T]: `(map f arr)`,
            # `(filter f arr)`, `(fold f init arr)`, `(any f arr)`,
            # `(all f arr)`, `(zip arr1 arr2)` — matching main.no's
            # documented call form (array/arrays last). The array operand
            # may be any array-valued expression (an inline literal, or a
            # nested HOF call from a `|>` pipeline), not only a named
            # binding -- see `_hof_array_arg`. A user function of the same
            # name shadows the builtin.
            if name in ("map", "filter", "flat_map", "any", "all") and len(node.args) == 2:
                arr_name = None if name in self._fn_sigs else self._hof_array_arg(node.args[1])
                if arr_name is not None:
                    if name == "map":
                        return self._emit_hof_map(node.args[0], arr_name)
                    if name == "filter":
                        return self._emit_hof_filter(node.args[0], arr_name)
                    if name == "flat_map":
                        return self._emit_hof_flat_map(node.args[0], arr_name)
                    return self._emit_hof_any_all(node.args[0], arr_name, is_all=(name == "all"))
            if name == "fold" and len(node.args) == 3 and name not in self._fn_sigs:
                arr_name = self._hof_array_arg(node.args[2])
                if arr_name is not None:
                    return self._emit_hof_fold(node.args[0], node.args[1], arr_name)
            if name == "zip" and len(node.args) == 2 and name not in self._fn_sigs:
                arr1 = self._hof_array_arg(node.args[0])
                arr2 = self._hof_array_arg(node.args[1]) if arr1 is not None else None
                if arr1 is not None and arr2 is not None:
                    return self._emit_hof_zip(arr1, arr2)
            if name in ("min", "max") and len(node.args) == 2 and name not in self._fn_sigs:
                return self._emit_min_max(name, node.args)
            # Operators
            if name in ("+", "-", "*", "/", "%"):
                return self._emit_arith(name, node.args)
            if name in ("==", "!=", "<", ">", "<=", ">="):
                return self._emit_cmp(name, node.args)
            if name in ("&&", "||", "!"):
                return self._emit_bool_op(name, node.args)
            # Bitwise ops. `&` is overloaded with borrow (see below) --
            # disambiguated by arity, same convention typeck uses: 2 args
            # is bitwise AND, 1 arg is a borrow.
            if name in ("&", "|", "^") and len(node.args) == 2:
                return self._emit_bitwise(name, node.args)
            if name in ("<<", ">>") and len(node.args) == 2:
                return self._emit_shift(name, node.args)
            if name == "~" and len(node.args) == 1:
                return self._emit_bitnot(node.args[0])
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
            # A variant's payload may be passed by field name, as main.no
            # declares them -- `(Math inner:e)` for `(Math inner:MathError)` --
            # in declaration order. Keyword args used to reach `_emit_expr`
            # as-is and crash.
            variant_args = [
                a.value if isinstance(a, N.KeywordArg) and a.value is not None else a
                for a in node.args
            ]
            if name in self._generic_variant_ctors:
                mangled_vname = self._resolve_generic_variant(name, variant_args)
                if mangled_vname:
                    return self._emit_variant_construct(mangled_vname, variant_args)
            # Sum type variant constructor (already monomorphized or
            # belonging to a non-generic sum type)
            if name in self._variant_ctors:
                return self._emit_variant_construct(name, variant_args)
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
            # dyn trait object dispatch: `(method dyn_val ...)` — the
            # receiver's concrete type is unknown until runtime, so the
            # method is looked up in its vtable instead of statically.
            # Checked before the static path below since a dyn binding
            # has no fixed struct name to look up in `_method_impls`.
            if node.args:
                probe = self._unwrap_borrow(node.args[0])
                if isinstance(probe, N.Ident) and probe.name in self._env_dyn_trait:
                    trait_name = self._env_dyn_trait[probe.name]
                    method_names = [m.name for m in self._traits.get(trait_name, [])]
                    if name in method_names:
                        return self._emit_dyn_call(probe.name, trait_name, name, node.args)
            # Inherent method dispatch: `(method receiver ...)` —
            # look up the receiver's struct type and try its impl.
            if name not in self._fn_sigs and node.args:
                probe = self._unwrap_borrow(node.args[0])
                sn = self._infer_nyet_type_name(probe)
                if sn is None and isinstance(probe, N.Ident):
                    sn = self._env_struct_name.get(probe.name)
                if sn is not None and (sn, name) in self._method_impls:
                    return self._emit_user_call(self._method_impls[(sn, name)], node.args)
            # v0.6: indirect call through a fn-typed binding (parameter
            # or let bound to a lifted lambda).
            if name in self._env_fn_sig:
                return self._emit_indirect_call(name, node.args)
            # User function
            return self._emit_user_call(name, node.args)
        if isinstance(node.head, N.Path) and len(node.head.segments) == 2:
            # `(Type/name ...)` — a self-less `impl` function (an
            # associated/"static" constructor, e.g. `(fn origin () ->
            # Point ...)` inside `impl Point`) called by its
            # type-qualified path. There's no receiver argument to
            # dispatch inherent-method lookup from (that path only
            # triggers for `(method receiver ...)`), so this is the
            # only call syntax such a method has at all — without this,
            # `_emit_call` fell through to the final `return None`
            # below, silently producing no call whatsoever.
            target_name, method_name = node.head.segments
            mangled = self._method_impls.get((target_name, method_name))
            if mangled is not None:
                return self._emit_user_call(mangled, node.args)
        unqualified = self._unqualify_module_call(node)
        if unqualified is not None:
            return self._emit_call(unqualified)
        if (
            isinstance(node.head, (N.FieldAccess, N.Call))
            and len(node.args) == 1
            and self._array_elem_ty_of_expr(node.head) is not None
        ):
            # `((. v data) i)` or `((grid i) j)` -- indexing an
            # `Array[T]` value produced by a further expression. See
            # `_emit_array_index_nested`.
            return self._emit_array_index_nested(node.head, node.args[0])
        head = node.head
        if isinstance(head, N.Path):
            head_desc = "path '" + "/".join(head.segments) + "'"
        else:
            head_desc = type(head).__name__
        raise NotImplementedError(
            f"codegen: can't emit a call whose head is {head_desc} (span {node.span}) -- "
            f"this used to silently evaluate to nothing"
        )

    # ------------------------------------------------------------------
    # v0.3: Generic call inference helpers
    # ------------------------------------------------------------------

    def _monomorphize_fn_from_args(self, name: str, args: list[N.Expr]) -> str | None:
        """Infer type args for a generic fn call and monomorphize."""
        tmpl = self._fn_templates[name]
        type_args = self._infer_type_args_for_fn(tmpl, args)
        if type_args is None:
            return None
        return self._monomorphize_fn(name, type_args)

    def _monomorphize_struct_from_args(self, name: str, args: list[N.Expr]) -> str | None:
        tmpl = self._struct_templates[name]
        # Try to match field types with generic params
        type_args = self._infer_type_args_for_struct(tmpl, args)
        if type_args is None:
            return None
        return self._monomorphize_struct(name, type_args)

    def _sum_type_args_from_type_node(
        self, tn: N.TypeNode | None, sum_name: str
    ) -> tuple[str, ...] | None:
        """If `tn` is `sum_name[concrete_args...]` (or `&sum_name[...]`),
        return the concrete Nyet type-arg names -- used as a fallback
        source of generic type args a variant's own field types can't
        fully determine (see `_resolve_generic_variant`)."""
        if isinstance(tn, N.RefType):
            return self._sum_type_args_from_type_node(tn.inner, sum_name)
        if isinstance(tn, N.GenericType):
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name == sum_name and tn.args:
                return tuple(self._nyet_type_name_of_node(a) or "unk" for a in tn.args)
        return None

    def _resolve_generic_variant(self, vname: str, args: list[N.Expr]) -> str | None:
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
            type_args = self._infer_type_args_for_variant(tmpl, variant_types, args)
            if type_args is None:
                # A variant that doesn't reference every one of the sum
                # type's generic params in its own field types (e.g.
                # `Err`'s payload type never appears in `Ok`'s fields)
                # can't be fully resolved from the constructor call's
                # own arguments alone -- fall back to the enclosing
                # function's declared return type, when the variant
                # construction is (as it almost always is) that
                # function's return value. Without this, unresolved
                # inference silently fell through to the bare-name
                # `_variant_ctors[vname]` lookup below in `_emit_call`,
                # which whichever sum type instantiation registered
                # LAST for this variant name always won -- a silent
                # wrong-type miscompile (or an outright ill-typed store)
                # for every OTHER instantiation of the same generic sum
                # type in the same program.
                type_args = self._sum_type_args_from_type_node(
                    self._current_fn_return_type_node, sum_name
                )
                if type_args is None or len(type_args) != len(tmpl.generics):
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

    def _find_variant_ctor_for(self, sum_type_name: str, vname: str) -> str | None:
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

    def _infer_type_args_for_fn(self, tmpl: N.FnDecl, args: list[N.Expr]) -> tuple[str, ...] | None:
        return self._infer_type_args_from_params(tmpl.generics, tmpl.params, args)

    def _infer_type_args_for_struct(
        self, tmpl: N.StructDecl, args: list[N.Expr]
    ) -> tuple[str, ...] | None:
        # `_infer_type_args_from_params` unifies `tmpl.fields[i]` against
        # `args[i]` purely positionally, so a keyword-style constructor
        # call (`(Pair first:a second:b)` -- the primary documented
        # struct-construction style, per main.no's own examples) needs
        # its args reordered into declaration order first. A previous
        # version of this method instead filtered keyword args out
        # entirely, so a generic struct constructed with ALL keyword
        # args (no positional args left at all) had nothing to unify
        # against and could never infer its type args -- the call fell
        # through every other dispatch case in `_emit_call` and was
        # misparsed as an ordinary function call, whose args (still
        # `KeywordArg` nodes) then hit the `_emit_expr` catch-all.
        ordered_args = self._reorder_ctor_args(tmpl.fields, args)
        return self._infer_type_args_from_params(tmpl.generics, tmpl.fields, ordered_args)

    def _reorder_ctor_args(self, fields: list, args: list[N.Expr]) -> list[N.Expr]:
        """Reorder a struct constructor's actual arguments (a mix of
        positional and keyword-style) into declaration order, matching
        `_emit_struct_construct`'s own by-name/by-position field
        matching -- needed so callers that unify positionally (generic
        type-arg inference) work regardless of whether the call used
        keyword args, positional args, or a mix.
        """
        by_name: dict[str, N.Expr] = {}
        positional: list[N.Expr] = []
        for a in args:
            if isinstance(a, N.KeywordArg):
                if a.value is not None:
                    by_name[a.name] = a.value
            else:
                positional.append(a)
        ordered: list[N.Expr] = []
        pos_i = 0
        for f in fields:
            if f.name in by_name:
                ordered.append(by_name[f.name])
            elif pos_i < len(positional):
                ordered.append(positional[pos_i])
                pos_i += 1
        return ordered

    def _infer_type_args_for_variant(
        self,
        tmpl: N.TypeDecl,
        variant_types: list[N.TypeNode],
        args: list[N.Expr],
    ) -> tuple[str, ...] | None:
        # Build fake params with the variant's field types
        fake_params = [N.Param(tmpl.span, f"_{i}", t) for i, t in enumerate(variant_types)]
        return self._infer_type_args_from_params(tmpl.generics, fake_params, args)

    def _infer_type_args_from_params(
        self,
        generics: list[N.GenericParam],
        params: list,
        args: list[N.Expr],
    ) -> tuple[str, ...] | None:
        """Infer concrete type args by unifying each param's declared type
        against its argument's concrete type.

        Recurses into `Array[T]` and closure `(fn (T) -> R)` shapes via
        `_unify_param_type`, not just a param whose type IS directly the
        generic name -- needed for anything like
        `(fn f[T] (pred:(fn (T) -> bool) arr:Array[T]) -> Array[T] ...)`,
        which previously left `T` unresolved and silently failed to
        monomorphize (the call site would reference an emitted function
        that never got emitted).
        """
        if not generics:
            return ()
        generic_names = {gp.name for gp in generics}
        resolved: dict[str, str] = {}
        for i, p in enumerate(params):
            if i >= len(args):
                break
            self._unify_param_type(p.type, args[i], generic_names, resolved)
        result = []
        for gp in generics:
            if gp.name not in resolved:
                return None
            result.append(resolved[gp.name])
        return tuple(result)

    def _unify_param_type(
        self,
        pattern: N.TypeNode | None,
        arg: N.Expr,
        generic_names: set[str],
        resolved: dict[str, str],
    ) -> None:
        """Match a declared param TypeNode against its concrete argument
        expression, binding any generic names found in `pattern` into
        `resolved` (first occurrence wins, matching the old direct-match
        behavior for the simple case).
        """
        if pattern is None:
            return
        if isinstance(pattern, N.RefType):
            self._unify_param_type(pattern.inner, arg, generic_names, resolved)
            return
        if isinstance(pattern, N.NamedType) and pattern.name in generic_names:
            if pattern.name not in resolved:
                resolved[pattern.name] = self._infer_nyet_type_from_arg(arg)
            return
        if (
            isinstance(pattern, N.GenericType)
            and isinstance(pattern.base, N.NamedType)
            and pattern.base.name == "Array"
            and len(pattern.args) == 1
        ):
            # Element type isn't recoverable as a Nyet name for a ptr-
            # backed element (string/struct/nested array all share the
            # same "ptr" LLVM representation) -- _nyet_from_llvm's
            # ptr->"string" default applies here too, same as every
            # other lossy LLVM->Nyet-name site in this file. Primitive
            # element types (i32/i64/f64/f32/bool/i8/i16) round-trip
            # exactly, which covers main.no's own generic-HOF examples.
            unwrapped = self._unwrap_borrow(arg)
            elem_nyet = (
                self._env_array_elem_nyet.get(unwrapped.name)
                if isinstance(unwrapped, N.Ident)
                else None
            )
            elem_llvm_ty = self._array_expr_elem_ty(unwrapped)
            if elem_nyet is not None:
                self._unify_type_against_nyet(pattern.args[0], elem_nyet, generic_names, resolved)
            elif elem_llvm_ty is not None:
                self._unify_type_against_llvm(
                    pattern.args[0], elem_llvm_ty, generic_names, resolved
                )
            return
        if isinstance(pattern, N.FnType):
            # A lambda's declared types are Nyet types, which -- unlike its
            # LLVM signature -- tell a struct or sum type apart from a string.
            decl = self._lifted_decls.get(arg.name) if isinstance(arg, N.Ident) else None
            if decl is not None:
                for pp, param in zip(pattern.params, decl.params, strict=False):
                    self._unify_type_against_nyet(
                        pp, self._nyet_type_name(param.type), generic_names, resolved
                    )
                if pattern.ret is not None and decl.return_type is not None:
                    self._unify_type_against_nyet(
                        pattern.ret,
                        self._nyet_type_name(decl.return_type),
                        generic_names,
                        resolved,
                    )
                return
            callee_sig = None
            if isinstance(arg, N.Ident):
                if arg.name in self._fn_sigs:
                    callee_sig = self._fn_sigs[arg.name]
                elif arg.name in self._env_fn_sig:
                    callee_sig = self._env_fn_sig[arg.name]
            if callee_sig is not None:
                param_tys, ret_ty = callee_sig
                for pp, at in zip(pattern.params, param_tys, strict=False):
                    self._unify_type_against_llvm(pp, at, generic_names, resolved)
                if pattern.ret is not None:
                    self._unify_type_against_llvm(pattern.ret, ret_ty, generic_names, resolved)
            return
        if isinstance(pattern, N.TupleType):
            unwrapped = self._unwrap_borrow(arg)
            if isinstance(unwrapped, N.TupleLit):
                for pe, ae in zip(pattern.elements, unwrapped.elements, strict=False):
                    self._unify_param_type(pe, ae, generic_names, resolved)
            elif isinstance(unwrapped, N.Ident) and unwrapped.name in self._env_tuple_types:
                self._unify_type_against_nyet(
                    pattern, self._env_tuple_types[unwrapped.name], generic_names, resolved
                )
            return
        if isinstance(pattern, N.GenericType):
            # e.g. `result:Result[T E]` against a value of a monomorphized
            # generic type such as `Result__f64_MathError`.
            self._unify_type_against_nyet(
                pattern,
                self._infer_nyet_type_name(self._unwrap_borrow(arg)),
                generic_names,
                resolved,
            )
            return

    def _unify_type_against_nyet(
        self,
        pattern: N.TypeNode | None,
        nyet: str | None,
        generic_names: set[str],
        resolved: dict[str, str],
    ) -> None:
        """Like `_unify_param_type`, against a concrete Nyet type name --
        which, unlike an LLVM type, distinguishes struct/sum/string types and
        (for a monomorphized generic type) still carries its type arguments."""
        if pattern is None or nyet is None:
            return
        if isinstance(pattern, N.RefType):
            self._unify_type_against_nyet(pattern.inner, nyet, generic_names, resolved)
            return
        if isinstance(pattern, N.NamedType) and pattern.name in generic_names:
            resolved.setdefault(pattern.name, nyet)
            return
        if isinstance(pattern, N.GenericType) and isinstance(
            pattern.base, (N.NamedType, N.PrimType)
        ):
            origin = next(
                (key for key, mangled in self._mono_types.items() if mangled == nyet), None
            )
            if origin is not None and origin[0] == pattern.base.name:
                for pa, targ in zip(pattern.args, origin[1], strict=False):
                    self._unify_type_against_nyet(pa, targ, generic_names, resolved)
            return
        if isinstance(pattern, N.TupleType) and nyet in self._structs:
            fields = self._structs[nyet]
            field_nyet = self._struct_field_nyet.get(nyet, {})
            for i, pe in enumerate(pattern.elements):
                if i < len(fields):
                    elem = field_nyet.get(fields[i][0]) or self._nyet_from_llvm(fields[i][1])
                    self._unify_type_against_nyet(pe, elem, generic_names, resolved)

    def _unify_type_against_llvm(
        self,
        pattern: N.TypeNode | None,
        llvm_ty: str,
        generic_names: set[str],
        resolved: dict[str, str],
    ) -> None:
        """Like `_unify_param_type` but for recursion sites (closure
        params/return) where only an LLVM type string is available, not
        a full argument expression to re-inspect."""
        if pattern is None:
            return
        if isinstance(pattern, N.RefType):
            self._unify_type_against_llvm(pattern.inner, llvm_ty, generic_names, resolved)
            return
        if isinstance(pattern, N.NamedType) and pattern.name in generic_names:
            if pattern.name not in resolved:
                resolved[pattern.name] = self._nyet_from_llvm(llvm_ty)
            return

    # ------------------------------------------------------------------
    # out / err — the StdIO write path, shared by `io`'s stdout/stderr
    # fast path (see `_emit_io`) and `_emit_panic`.
    # ------------------------------------------------------------------

    def _emit_printf_call(self, file_ptr: str | None, fmt: str, arg_ty: str, val: str) -> None:
        """Emit one `printf`/`fprintf` call. `file_ptr` is None for stdout
        (plain `printf`) or a `FILE*` value for `fprintf` (stderr)."""
        tmp = self._fresh_tmp()
        if file_ptr is None:
            self._declare_printf()
            self._emit_line(f"{tmp} = call i32 (ptr, ...) @printf(ptr {fmt}, {arg_ty} {val})")
        else:
            self._declare_extern("declare i32 @fprintf(ptr, ptr, ...)")
            self._emit_line(
                f"{tmp} = call i32 (ptr, ptr, ...) @fprintf("
                f"ptr {file_ptr}, ptr {fmt}, {arg_ty} {val})"
            )

    def _emit_stderr_handle(self) -> str:
        """Cached `FILE*` for fd 2 (stderr), opened once via `fdopen` —
        mirrors `_emit_in`'s cached stdin handle rather than referencing
        the libc `stderr`/`__stderrp` global directly, whose exact symbol
        differs across glibc and macOS libc."""
        self._declare_extern("declare ptr @fdopen(i32, ptr)")
        self._declare_extern("@__nyet_stderr = internal global ptr null")
        cached = self._fresh_tmp()
        self._emit_line(f"{cached} = load ptr, ptr @__nyet_stderr")
        need_open = self._fresh_tmp()
        self._emit_line(f"{need_open} = icmp eq ptr {cached}, null")
        open_label = self._fresh_label("err_open")
        have_label = self._fresh_label("err_have")
        self._emit_line(f"br i1 {need_open}, label %{open_label}, label %{have_label}")

        self._emit_label(open_label)
        mode_name = self._get_format_string("w", "w_mode")
        opened = self._fresh_tmp()
        self._emit_line(f"{opened} = call ptr @fdopen(i32 2, ptr {mode_name})")
        self._emit_line(f"store ptr {opened}, ptr @__nyet_stderr")
        self._emit_line(f"br label %{have_label}")

        self._emit_label(have_label)
        handle = self._fresh_tmp()
        self._emit_line(f"{handle} = load ptr, ptr @__nyet_stderr")
        return handle

    def _emit_out(self, args: list[N.Expr], file_ptr: str | None = None) -> str | None:
        for arg in args:
            sn = self._infer_nyet_type_name(self._unwrap_borrow(arg))
            if sn is not None and sn in self._structs and (sn, "display") in self._method_impls:
                mangled = self._method_impls[(sn, "display")]
                val = self._emit_user_call(mangled, [arg])
                if val is not None:
                    self._emit_printf_call(file_ptr, self._get_fmt_str(), "ptr", val)
                continue
            val = self._emit_expr(arg)
            if val is None:
                if isinstance(arg, (N.UnitLit, N.Pass)):
                    continue
                # Used to be skipped silently -- dropped output, no error.
                raise NotImplementedError(
                    f"codegen: an `out` argument ({arg.span}) produced no value"
                )
            ty = self._infer_llvm_type(arg)
            if ty == "ptr":
                self._emit_printf_call(file_ptr, self._get_fmt_str(), "ptr", val)
            elif self._is_float(ty):
                fmt = self._get_fmt_f64()
                if ty == "float":
                    ext = self._fresh_tmp()
                    self._emit_line(f"{ext} = fpext float {val} to double")
                    val = ext
                self._emit_printf_call(file_ptr, fmt, "double", val)
            elif ty == "i64" and self._node_is_unsigned(arg):
                self._emit_printf_call(file_ptr, self._get_fmt_u64(), "i64", val)
            elif ty == "i64":
                self._emit_printf_call(file_ptr, self._get_fmt_i64(), "i64", val)
            elif self._node_is_char(arg):
                # `char` is stored as `i32` (a Unicode scalar value --
                # see CLAUDE.md), same LLVM shape as any other int, so
                # without this case it fell into the generic i32/`%d`
                # branch below and printed the numeric codepoint
                # instead of the character (confirmed: `(let ch:char
                # 65) (out! ch)` printed "65", not "A"). `%c` reads an
                # `int` varargs slot and takes its low byte, matching
                # `i32`'s width exactly -- no coercion needed.
                self._emit_printf_call(file_ptr, self._get_fmt_char(), "i32", val)
            elif self._node_is_unsigned(arg):
                val = self._coerce_int_to(val, ty, "i32", unsigned=True)
                self._emit_printf_call(file_ptr, self._get_fmt_u32(), "i32", val)
            else:
                val = self._coerce_int_to(val, ty, "i32")
                self._emit_printf_call(file_ptr, self._get_fmt_i32(), "i32", val)
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
        self._emit_line(f"{nl_tmp} = call i32 (ptr, ...) @printf(ptr {nl})")
        self._declare_extern("declare void @exit(i32)")
        self._emit_line("call void @exit(i32 1)")
        self._emit_line("unreachable")
        # Open a fresh dead block so any following emission still has a
        # valid insertion point — LLVM rejects instructions after a
        # terminator within the same basic block.
        dead = self._fresh_label("after_panic")
        self._emit_label(dead)
        return None

    def _emit_now(self) -> str:
        """`(now)` -- CPU time consumed by this process so far, in
        seconds, as an `f64`. Backed by libc `clock()` (`<time.h>`)
        rather than a wall-clock call (`gettimeofday`/`clock_gettime`):
        `clock()` is a single `clock_t` return value with no struct
        layout or platform-specific clock-ID constant to get wrong,
        making it the portable choice between macOS, Linux, and Windows
        for a first primitive. Intended for benchmarking CPU-bound Nyet
        code (loops, arithmetic, struct/array operations) — not for
        measuring real elapsed time around blocking I/O, which `clock()`
        doesn't count.

        `CLOCKS_PER_SEC` is 1000000 on glibc and macOS libc, but only
        1000 on the Windows CRT (millisecond, not microsecond,
        resolution) -- a well-known `clock()` portability gotcha.
        Nyet never cross-compiles (a frozen Windows build's bundled
        clang still runs ON Windows, targeting Windows), so `sys.platform`
        at codegen time already tells us which libc the emitted `.ll`
        will actually be linked against. Getting this wrong silently
        made every `busy_wait` call take ~1000x longer than intended on
        Windows (a 0.6s pause becomes a 10-minute one) -- easy to miss
        entirely in dev on macOS/Linux, since the divisor is only wrong
        for the platform this can't be tested on directly.
        """
        self._declare_extern("declare i64 @clock()")
        ticks = self._fresh_tmp()
        self._emit_line(f"{ticks} = call i64 @clock()")
        as_double = self._fresh_tmp()
        self._emit_line(f"{as_double} = sitofp i64 {ticks} to double")
        clocks_per_sec = 1000.0 if _sys.platform == "win32" else 1000000.0
        seconds = self._fresh_tmp()
        self._emit_line(f"{seconds} = fdiv double {as_double}, {clocks_per_sec}")
        return seconds

    # ------------------------------------------------------------------
    # in
    # ------------------------------------------------------------------

    # Recognized `(in TYPE)` primitive-type names — anything else in that
    # argument position is an explicit channel (`(in myfile)`), not a
    # parse-target type.
    _IN_TYPE_NAMES = {"i32", "int", "i64", "f64", "f32", "bool", "string"}

    def _channel_type_name(self, ch: N.Expr) -> str | None:
        probe = self._unwrap_borrow(ch)
        sn = self._infer_nyet_type_name(probe)
        if sn is None and isinstance(probe, N.Ident):
            sn = self._env_struct_name.get(probe.name)
        return sn

    def _emit_channel_write(self, ch: N.Expr, data: N.Expr) -> None:
        """Dispatch `write` on `ch` (FileIO builtin or any user-defined
        IOChannel implementer) and unwrap the Result, panicking on `Err`
        — the shared tail end of `io`'s write form (`(io ch data)`)."""
        sn = self._channel_type_name(ch)
        if sn == "FileIO":
            raw = self._emit_fileio_write([ch, data])
        elif sn is not None and (sn, "write") in self._method_impls:
            raw = self._emit_user_call(self._method_impls[(sn, "write")], [ch, data])
        else:
            raw = None
        if raw is not None:
            self._emit_io_unwrap_or_panic(raw, "write")

    def _emit_channel_read(self, ch: N.Expr) -> str | None:
        """Dispatch `read` on `ch` and unwrap the Result, panicking on
        `Err` — shared by `io`'s read form (`(io ch)`) and `(in ch)`."""
        sn = self._channel_type_name(ch)
        if sn == "FileIO":
            raw = self._emit_fileio_read([ch])
        elif sn is not None and (sn, "read") in self._method_impls:
            raw = self._emit_user_call(self._method_impls[(sn, "read")], [ch])
        else:
            return None
        if raw is None:
            return None
        return self._emit_io_unwrap_or_panic(raw, "read")

    def _emit_io(self, args: list[N.Expr]) -> str | None:
        """`io` — the unified channel verb. `(io ch)` reads, `(io ch data)`
        writes; arity distinguishes direction (the same asymmetry
        `(in)`/`(out x)` already had, just one name). Bare `out`/`err`
        are compile-time-recognized `StdIO` markers (see the design
        plan's scope note — they're never materialized as a runtime
        value in this pass), reusing the exact printf/fprintf logic the
        old `out` builtin had; any other channel goes through the
        generic write/read dispatch shared with `(in ch)` and direct
        `write`/`read`/`close` calls.
        """
        if not args:
            return None
        channel = self._unwrap_borrow(args[0])
        if isinstance(channel, N.Ident) and channel.name in ("out", "err", "in"):
            if len(args) == 1:
                if channel.name != "in":
                    self._emit_line("; io: read from a write-only StdIO channel is a no-op")
                    return self._get_string("")[0]
                return self._emit_in([])
            if channel.name == "in":
                self._emit_line("; io: write to a read-only StdIO channel is a no-op")
                return None
            file_ptr = self._emit_stderr_handle() if channel.name == "err" else None
            self._emit_out(args[1:], file_ptr)
            return None
        if len(args) == 1:
            return self._emit_channel_read(channel)
        self._emit_channel_write(channel, args[1])
        return None

    def _emit_in(self, args: list[N.Expr]) -> str | None:
        """`(in)` -- read one line from stdin.

        The FILE* stdin handle is opened via `fdopen` at most ONCE per
        process, cached in a global. A `(in)` call site inside a `loop`
        only appears once in the emitted IR, but that block runs on
        every iteration -- calling `fdopen(0, "r")` again on every one
        of those runtime iterations used to open a brand new stdio
        buffer on fd 0 each time. Because stdio's first `fgets` on a
        fresh buffer greedily reads ahead past the first line, and that
        buffered-but-unread remainder was discarded the moment the
        FILE* was abandoned at the end of the call, this silently
        dropped every line after the first. Once stdin was actually
        exhausted, `fgets` returned NULL (previously unchecked here),
        leaving `buf`'s stack memory untouched -- so every call after
        the first replayed the first line forever instead of the loop
        ever observing EOF. Caching the handle fixes the dropped-lines
        half; the EOF check below (empty string on NULL) fixes the
        replay-forever half.

        The read buffer itself is heap- (not stack-) allocated for the
        same reason every other Nyet `string` value is static or heap
        data: a `string` is meant to survive a function return (stored,
        passed on, returned again) like any other owned value. A stack
        `alloca` here would hand back a pointer into the current
        function's frame, which is a dangling pointer to the caller
        the moment that frame is popped -- e.g. any `(fn read_line ()
        -> string (in))`-style wrapper, an entirely ordinary pattern
        for prompting on a line of input, would silently read
        clobbered stack memory back out of it.
        """
        if len(args) == 1 and not (
            isinstance(args[0], N.Ident) and args[0].name in self._IN_TYPE_NAMES
        ):
            # Explicit-channel read: `(in myfile)`, distinct from
            # `(in i32)` above (a primitive-type name).
            return self._emit_channel_read(args[0])
        self._declare_extern("declare ptr @fgets(ptr, i32, ptr)")
        self._declare_extern("declare ptr @fdopen(i32, ptr)")
        self._declare_extern("declare ptr @malloc(i64)")
        self._declare_extern("@__nyet_stdin = internal global ptr null")
        self._declare_printf()

        buf_ptr = self._fresh_tmp()
        self._emit_line(f"{buf_ptr} = call ptr @malloc(i64 256)")

        cached = self._fresh_tmp()
        self._emit_line(f"{cached} = load ptr, ptr @__nyet_stdin")
        need_open = self._fresh_tmp()
        self._emit_line(f"{need_open} = icmp eq ptr {cached}, null")
        open_label = self._fresh_label("in_open")
        have_label = self._fresh_label("in_have")
        self._emit_line(f"br i1 {need_open}, label %{open_label}, label %{have_label}")

        self._emit_label(open_label)
        mode_name = self._get_format_string("r", "r_mode")
        opened = self._fresh_tmp()
        self._emit_line(f"{opened} = call ptr @fdopen(i32 0, ptr {mode_name})")
        self._emit_line(f"store ptr {opened}, ptr @__nyet_stdin")
        self._emit_line(f"br label %{have_label}")

        self._emit_label(have_label)
        stdin_fp = self._fresh_tmp()
        self._emit_line(f"{stdin_fp} = load ptr, ptr @__nyet_stdin")
        fgets_ret = self._fresh_tmp()
        self._emit_line(f"{fgets_ret} = call ptr @fgets(ptr {buf_ptr}, i32 256, ptr {stdin_fp})")

        is_eof = self._fresh_tmp()
        self._emit_line(f"{is_eof} = icmp eq ptr {fgets_ret}, null")
        eof_label = self._fresh_label("in_eof")
        done_label = self._fresh_label("in_done")
        self._emit_line(f"br i1 {is_eof}, label %{eof_label}, label %{done_label}")

        self._emit_label(eof_label)
        self._emit_line(f"store i8 0, ptr {buf_ptr}")
        self._emit_line(f"br label %{done_label}")

        self._emit_label(done_label)

        # `fgets` keeps the trailing newline (and a preceding `\r` on
        # CRLF input) in the buffer -- every line `(in)` read used to
        # come back with it still attached, so `(== line "quit")`
        # could never match a line actually typed as "quit". Truncate
        # at the first line-ending byte, same as a real line-reading
        # API would hand back.
        self._declare_extern("declare i64 @strcspn(ptr, ptr)")
        line_endings = self._get_format_string("\r\n", "in_line_endings")
        cut_off = self._fresh_tmp()
        self._emit_line(f"{cut_off} = call i64 @strcspn(ptr {buf_ptr}, ptr {line_endings})")
        cut_ptr = self._fresh_tmp()
        self._emit_line(f"{cut_ptr} = getelementptr i8, ptr {buf_ptr}, i64 {cut_off}")
        self._emit_line(f"store i8 0, ptr {cut_ptr}")

        target_type = "ptr"
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
        self._emit_line(f"{val64} = call i64 @strtol(ptr {buf_ptr}, ptr {endptr}, i32 10)")
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
        self._emit_line(f"br i1 {valid}, label %{ok_label}, label %{err_label}")

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
        self._emit_line(f"{seek_rc} = call i32 @fseek(ptr {h}, i64 0, i32 2)")
        size = self._fresh_tmp()
        self._emit_line(f"{size} = call i64 @ftell(ptr {h})")
        self._emit_line(f"call void @rewind(ptr {h})")
        size_p1 = self._fresh_tmp()
        self._emit_line(f"{size_p1} = add i64 {size}, 1")
        buf = self._fresh_tmp()
        self._emit_line(f"{buf} = call ptr @malloc(i64 {size_p1})")
        nread = self._fresh_tmp()
        self._emit_line(f"{nread} = call i64 @fread(ptr {buf}, i64 1, i64 {size}, ptr {h})")
        end = self._fresh_tmp()
        self._emit_line(f"{end} = getelementptr i8, ptr {buf}, i64 {size}")
        self._emit_line(f"store i8 0, ptr {end}")
        return buf

    def _emit_split_line_copy(
        self, start_slot: str, stop_i64: str, out_base: str, n_slot: str
    ) -> None:
        """Copy bytes `[*start_slot, stop_i64)` into a fresh malloc'd
        null-terminated buffer and append it to the `Array[string]`
        being built at `out_base`, via `n_slot` (the running output
        count, also the next write index) -- a helper for
        `_emit_file_read_lines`, which calls this once per line found
        (mid-buffer, at each `\\n`) and once more for a final line with
        no trailing newline, if there is one."""
        self._declare_extern("declare ptr @memcpy(ptr, ptr, i64)")
        ls = self._fresh_tmp()
        self._emit_line(f"{ls} = load ptr, ptr {start_slot}")
        ls_int = self._fresh_tmp()
        self._emit_line(f"{ls_int} = ptrtoint ptr {ls} to i64")
        line_len = self._fresh_tmp()
        self._emit_line(f"{line_len} = sub i64 {stop_i64}, {ls_int}")
        line_len_p1 = self._fresh_tmp()
        self._emit_line(f"{line_len_p1} = add i64 {line_len}, 1")
        line_buf = self._fresh_tmp()
        self._emit_line(f"{line_buf} = call ptr @malloc(i64 {line_len_p1})")
        self._emit_line(f"call ptr @memcpy(ptr {line_buf}, ptr {ls}, i64 {line_len})")
        line_end = self._fresh_tmp()
        self._emit_line(f"{line_end} = getelementptr i8, ptr {line_buf}, i64 {line_len}")
        self._emit_line(f"store i8 0, ptr {line_end}")
        n_val = self._fresh_tmp()
        self._emit_line(f"{n_val} = load i64, ptr {n_slot}")
        slot_ptr = self._fresh_tmp()
        self._emit_line(f"{slot_ptr} = getelementptr ptr, ptr {out_base}, i64 {n_val}")
        self._emit_line(f"store ptr {line_buf}, ptr {slot_ptr}")
        n_next = self._fresh_tmp()
        self._emit_line(f"{n_next} = add i64 {n_val}, 1")
        self._emit_line(f"store i64 {n_next}, ptr {n_slot}")

    def _emit_file_read_lines(self, args: list[N.Expr]) -> str | None:
        """`(file_read_lines handle) -> Array[string]` -- reads the
        whole file (like `file_read_all`) and splits it on `\\n` into
        one malloc'd copy per line. Mirrors Python's `str.splitlines`:
        a final trailing newline does not produce a trailing empty
        element, and an empty file yields a zero-length array.

        The output array is over-allocated at `(file size + 1)`
        elements -- the true worst case, if every byte were a newline
        -- then the header is corrected to the real line count once
        the single pass is done. Wastes at most 8 bytes per byte of
        file content, which is fine for the small line-oriented files
        (config, save-data) this exists for; not meant for bulk data.

        There's no general `split`/substring primitive in Nyet yet
        (see CONTINUATION_PLAN.md) -- this is a narrower, purpose-built
        builtin for exactly the "read a file back as one row per line"
        shape, added because minibase's save/load needed it and
        nothing already in the language could express it: a `char` has
        no route back to a one-character `string` (no cast, and `+`
        only concatenates two strings), so a general split() written
        in Nyet source itself isn't reachable without this.
        """
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

        # Read the whole file into a null-terminated buffer -- same as
        # file_read_all.
        self._emit_line(f"call i32 @fseek(ptr {h}, i64 0, i32 2)")
        size = self._fresh_tmp()
        self._emit_line(f"{size} = call i64 @ftell(ptr {h})")
        self._emit_line(f"call void @rewind(ptr {h})")
        size_p1 = self._fresh_tmp()
        self._emit_line(f"{size_p1} = add i64 {size}, 1")
        buf = self._fresh_tmp()
        self._emit_line(f"{buf} = call ptr @malloc(i64 {size_p1})")
        self._emit_line(f"call i64 @fread(ptr {buf}, i64 1, i64 {size}, ptr {h})")
        end_ptr = self._fresh_tmp()
        self._emit_line(f"{end_ptr} = getelementptr i8, ptr {buf}, i64 {size}")
        self._emit_line(f"store i8 0, ptr {end_ptr}")
        end_int = self._fresh_tmp()
        self._emit_line(f"{end_int} = ptrtoint ptr {end_ptr} to i64")

        # Over-allocated Array[string] output: [i64 len][ptr elements...].
        cap_bytes = self._fresh_tmp()
        self._emit_line(f"{cap_bytes} = mul i64 {size_p1}, 8")
        out_total = self._fresh_tmp()
        self._emit_line(f"{out_total} = add i64 {cap_bytes}, 8")
        out = self._fresh_tmp()
        self._emit_line(f"{out} = call ptr @malloc(i64 {out_total})")
        out_base = self._array_data_base(out)

        # Loop state: byte cursor `i`, current-line-start pointer, and
        # the running output count (also next write index).
        i_slot = self._emit_alloca("i64")
        self._emit_line(f"store i64 0, ptr {i_slot}")
        start_slot = self._emit_alloca("ptr")
        self._emit_line(f"store ptr {buf}, ptr {start_slot}")
        n_slot = self._emit_alloca("i64")
        self._emit_line(f"store i64 0, ptr {n_slot}")

        cond = self._fresh_label("lines_cond")
        body = self._fresh_label("lines_body")
        found_nl = self._fresh_label("lines_nl")
        advance = self._fresh_label("lines_advance")
        after_loop = self._fresh_label("lines_after_loop")
        tail_then = self._fresh_label("lines_tail")
        tail_merge = self._fresh_label("lines_tail_merge")

        self._emit_line(f"br label %{cond}")
        self._emit_label(cond)
        i_val = self._fresh_tmp()
        self._emit_line(f"{i_val} = load i64, ptr {i_slot}")
        in_bounds = self._fresh_tmp()
        self._emit_line(f"{in_bounds} = icmp slt i64 {i_val}, {size}")
        self._emit_line(f"br i1 {in_bounds}, label %{body}, label %{after_loop}")

        self._emit_label(body)
        ch_ptr = self._fresh_tmp()
        self._emit_line(f"{ch_ptr} = getelementptr i8, ptr {buf}, i64 {i_val}")
        ch = self._fresh_tmp()
        self._emit_line(f"{ch} = load i8, ptr {ch_ptr}")
        is_nl = self._fresh_tmp()
        self._emit_line(f"{is_nl} = icmp eq i8 {ch}, 10")
        self._emit_line(f"br i1 {is_nl}, label %{found_nl}, label %{advance}")

        self._emit_label(found_nl)
        ch_int = self._fresh_tmp()
        self._emit_line(f"{ch_int} = ptrtoint ptr {ch_ptr} to i64")
        self._emit_split_line_copy(start_slot, ch_int, out_base, n_slot)
        i_val_p1 = self._fresh_tmp()
        self._emit_line(f"{i_val_p1} = add i64 {i_val}, 1")
        new_start = self._fresh_tmp()
        self._emit_line(f"{new_start} = getelementptr i8, ptr {buf}, i64 {i_val_p1}")
        self._emit_line(f"store ptr {new_start}, ptr {start_slot}")
        self._emit_line(f"br label %{advance}")

        self._emit_label(advance)
        i_inc = self._fresh_tmp()
        self._emit_line(f"{i_inc} = add i64 {i_val}, 1")
        self._emit_line(f"store i64 {i_inc}, ptr {i_slot}")
        self._emit_line(f"br label %{cond}")

        # A final line with no trailing newline still needs flushing.
        self._emit_label(after_loop)
        ls_final = self._fresh_tmp()
        self._emit_line(f"{ls_final} = load ptr, ptr {start_slot}")
        ls_final_int = self._fresh_tmp()
        self._emit_line(f"{ls_final_int} = ptrtoint ptr {ls_final} to i64")
        has_tail = self._fresh_tmp()
        self._emit_line(f"{has_tail} = icmp slt i64 {ls_final_int}, {end_int}")
        self._emit_line(f"br i1 {has_tail}, label %{tail_then}, label %{tail_merge}")
        self._emit_label(tail_then)
        self._emit_split_line_copy(start_slot, end_int, out_base, n_slot)
        self._emit_line(f"br label %{tail_merge}")
        self._emit_label(tail_merge)

        n_final = self._fresh_tmp()
        self._emit_line(f"{n_final} = load i64, ptr {n_slot}")
        self._emit_line(f"store i64 {n_final}, ptr {out}")
        return out

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
        self._emit_line(f"{wrote} = call i64 @fwrite(ptr {text}, i64 1, i64 {n}, ptr {h})")
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
    # IOChannel — WriteResult/ReadResult/CloseResult construction and
    # unwrapping for builtin channel implementers (StdIO, FileIO). Each
    # operation gets its own concrete (non-generic) result type — see
    # the prelude source comment in expand.py for why this isn't one
    # generic `Result[T E]` instantiated three ways. A user-defined
    # channel's own `write`/`read`/`close` instead go through ordinary
    # Nyet-source `(WriteOk ...)`/`(WriteErr ...)`-etc. call syntax in
    # its `impl` body, so it never needs these helpers.
    # ------------------------------------------------------------------

    # op -> (result sum type, Ok variant name, Err variant name). Ok is
    # always tag 0 / Err tag 1 by construction (declaration order in the
    # prelude source), so these are read from `_sum_types` directly
    # rather than round-tripped through `_variant_ctors`.
    _IO_RESULT_TYPES = {
        "write": ("WriteResult", "WriteOk", "WriteErr"),
        "read": ("ReadResult", "ReadOk", "ReadErr"),
        "close": ("CloseResult", "CloseOk", "CloseErr"),
    }

    def _emit_io_ok(self, op: str, val: str | None) -> str | None:
        """Construct `<op>Ok(val)` (e.g. `WriteOk(val)`)."""
        sum_name, _, _ = self._IO_RESULT_TYPES[op]
        variants = self._sum_types.get(sum_name)
        if variants is None:
            return None
        _, payload_types = variants[0]
        values = [(payload_types[0], val)] if payload_types and val is not None else []
        return self._emit_variant_construct_raw(sum_name, 0, payload_types, values)

    def _emit_io_err(self, op: str, msg_ptr: str) -> str | None:
        """Construct `<op>Err(Other(msg_ptr))` (e.g. `WriteErr(...)`)."""
        sum_name, _, _ = self._IO_RESULT_TYPES[op]
        variants = self._sum_types.get(sum_name)
        if variants is None or "Other" not in self._variant_ctors:
            return None
        io_error_sum, other_tag = self._variant_ctors["Other"]
        io_variants = self._sum_types[io_error_sum]
        _, other_payload_types = io_variants[other_tag]
        other_values = [(other_payload_types[0], msg_ptr)] if other_payload_types else []
        err_val = self._emit_variant_construct_raw(
            io_error_sum, other_tag, other_payload_types, other_values
        )

        _, err_payload_types = variants[1]
        values = [(err_payload_types[0], err_val)] if err_payload_types else []
        return self._emit_variant_construct_raw(sum_name, 1, err_payload_types, values)

    def _emit_io_unwrap_or_panic(self, result_ptr: str, op: str) -> str | None:
        """Given a `<op>Result` value, return its unwrapped Ok payload, or
        panic. This is `io`'s sugar contract (see main.no's "IO Channels"
        section and the IOChannel design plan) — a caller who wants the
        actual `IOError` instead calls `write`/`read`/`close` directly
        and `match`es the result themselves, so the panic message here
        is intentionally generic rather than threading the real error
        text through."""
        sum_name, _, _ = self._IO_RESULT_TYPES[op]
        variants = self._sum_types.get(sum_name)
        if variants is None:
            return result_ptr

        tag_ptr = self._fresh_tmp()
        self._emit_line(
            f"{tag_ptr} = getelementptr inbounds %{sum_name}, ptr {result_ptr}, i32 0, i32 0"
        )
        tag = self._fresh_tmp()
        self._emit_line(f"{tag} = load i32, ptr {tag_ptr}")
        is_ok = self._fresh_tmp()
        self._emit_line(f"{is_ok} = icmp eq i32 {tag}, 0")
        ok_label = self._fresh_label("io_ok")
        err_label = self._fresh_label("io_err")
        self._emit_line(f"br i1 {is_ok}, label %{ok_label}, label %{err_label}")

        self._emit_label(err_label)
        self._declare_printf()
        msg = self._get_format_string("io: operation failed\n", "io_panic_msg")
        panic_tmp = self._fresh_tmp()
        self._emit_line(f"{panic_tmp} = call i32 (ptr, ...) @printf(ptr {msg})")
        self._declare_extern("declare void @exit(i32)")
        self._emit_line("call void @exit(i32 1)")
        self._emit_line("unreachable")

        self._emit_label(ok_label)
        _, ok_payload_types = variants[0]
        if not ok_payload_types:
            return None
        payload_ptr = self._fresh_tmp()
        self._emit_line(
            f"{payload_ptr} = getelementptr inbounds %{sum_name}, ptr {result_ptr}, i32 0, i32 1"
        )
        ok_val = self._fresh_tmp()
        self._emit_line(f"{ok_val} = load {ok_payload_types[0]}, ptr {payload_ptr}")
        return ok_val

    # ------------------------------------------------------------------
    # FileIO — a builtin struct implementing IOChannel (see design plan).
    # Constructor + write/read/close are hand-rolled here (like
    # file_open et al. above) rather than expressed as a real `impl` in
    # Nyet source, since dispatch for them is intercepted by name+receiver
    # type directly in `_emit_call` before the generic method-dispatch
    # path ever runs (see `_emit_call`).
    # ------------------------------------------------------------------

    _FILE_MODE_TO_C = {
        "Read": "r",
        "Write": "w",
        "Append": "a",
        "ReadWrite": "r+",
    }

    def _emit_fileio_open(self, args: list[N.Expr]) -> str | None:
        if len(args) != 2:
            return None
        self._declare_extern("declare ptr @fopen(ptr, ptr)")
        self._declare_extern("declare ptr @malloc(i64)")
        path = self._emit_expr(args[0])
        if path is None:
            return None
        mode_name = self._nullary_variant_name(args[1]) or "Read"
        c_mode = self._get_string(self._FILE_MODE_TO_C.get(mode_name, "r"))[0]
        handle = self._fresh_tmp()
        self._emit_line(f"{handle} = call ptr @fopen(ptr {path}, ptr {c_mode})")
        closed_flag = self._fresh_tmp()
        self._emit_line(f"{closed_flag} = icmp eq ptr {handle}, null")

        struct_name = "FileIO"
        ptr = self._heap_alloc_struct(struct_name, self._struct_size_bytes(struct_name))
        handle_ptr = self._fresh_tmp()
        self._emit_line(
            f"{handle_ptr} = getelementptr inbounds %{struct_name}, ptr {ptr}, i32 0, i32 0"
        )
        self._emit_line(f"store ptr {handle}, ptr {handle_ptr}")
        mode_ptr = self._fresh_tmp()
        self._emit_line(
            f"{mode_ptr} = getelementptr inbounds %{struct_name}, ptr {ptr}, i32 0, i32 1"
        )
        mode_val = self._emit_construct_nullary_mode("FileMode", mode_name)
        self._emit_line(f"store ptr {mode_val}, ptr {mode_ptr}")
        closed_ptr = self._fresh_tmp()
        self._emit_line(
            f"{closed_ptr} = getelementptr inbounds %{struct_name}, ptr {ptr}, i32 0, i32 2"
        )
        self._emit_line(f"store i1 {closed_flag}, ptr {closed_ptr}")
        return ptr

    def _nullary_variant_name(self, node: N.Expr) -> str | None:
        """`(Read)`/`(Write)`/etc. — a zero-arg sum-type variant call.
        Returns the variant's bare name (`"Read"`), or None."""
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            return node.head.name
        return None

    def _emit_construct_nullary_mode(self, sum_type_hint: str, vname: str) -> str:
        """Construct a zero-payload sum type variant value (e.g. `(Append)`
        of `FileMode`) directly from its name, for builtins that receive
        the variant name as a Python string rather than an AST call node."""
        key = f"{sum_type_hint}::{vname}"
        if key in self._variant_ctors:
            sum_name, tag_idx = self._variant_ctors[key]
        else:
            sum_name, tag_idx = self._variant_ctors[vname]
        return self._emit_variant_construct_raw(sum_name, tag_idx, (), [])

    def _emit_fileio_field(self, ch: N.Expr, field_idx: int) -> str:
        """GEP to a FileIO field (0=handle, 1=mode, 2=closed) without a load."""
        base = self._emit_expr(ch)
        fptr = self._fresh_tmp()
        self._emit_line(
            f"{fptr} = getelementptr inbounds %FileIO, ptr {base}, i32 0, i32 {field_idx}"
        )
        return fptr

    def _emit_fileio_closed(self, ch: N.Expr) -> tuple[str, str]:
        """Returns (is_closed_i1_value, handle_field_ptr) for a FileIO."""
        handle_ptr = self._emit_fileio_field(ch, 0)
        closed_ptr = self._emit_fileio_field(ch, 2)
        closed = self._fresh_tmp()
        self._emit_line(f"{closed} = load i1, ptr {closed_ptr}")
        return closed, handle_ptr

    def _emit_fileio_closed_guard(
        self,
        ch: N.Expr,
        prefix: str,
        op: str,
        err_msg: str,
        emit_ok: Callable[[str], str],
    ) -> str:
        """Shared shape for write/read/close: if the channel's `closed`
        flag is set, return `Err`; otherwise run `emit_ok(handle_ptr)`
        (the real libc operation, returning its own `Ok`/`Err` Result) and
        merge the two paths with a `phi`. `emit_ok` is assumed to leave
        control in the same block it started in (true for the simple
        libc-call + `_emit_io_ok`/`_emit_io_err` bodies below) — the same
        "no explicit current-block tracking" assumption the rest of this
        emitter already relies on for straight-line expression codegen.
        """
        handle_ptr = self._emit_fileio_field(ch, 0)
        closed_ptr = self._emit_fileio_field(ch, 2)
        closed = self._fresh_tmp()
        self._emit_line(f"{closed} = load i1, ptr {closed_ptr}")
        closed_label = self._fresh_label(f"{prefix}_closed")
        ok_label = self._fresh_label(f"{prefix}_ok")
        done_label = self._fresh_label(f"{prefix}_done")
        self._emit_line(f"br i1 {closed}, label %{closed_label}, label %{ok_label}")

        self._emit_label(closed_label)
        msg_ptr = self._get_string(err_msg)[0]
        err_result = self._emit_io_err(op, msg_ptr)
        self._emit_line(f"br label %{done_label}")

        self._emit_label(ok_label)
        ok_result = emit_ok(handle_ptr)
        self._emit_line(f"br label %{done_label}")

        self._emit_label(done_label)
        merged = self._fresh_tmp()
        self._emit_line(
            f"{merged} = phi ptr [ {err_result}, %{closed_label} ], [ {ok_result}, %{ok_label} ]"
        )
        return merged

    def _emit_fileio_write(self, args: list[N.Expr]) -> str | None:
        if len(args) != 2:
            return None
        ch, data = args

        def emit_ok(handle_ptr: str) -> str:
            self._declare_extern("declare i64 @strlen(ptr)")
            self._declare_extern("declare i64 @fwrite(ptr, i64, i64, ptr)")
            handle = self._fresh_tmp()
            self._emit_line(f"{handle} = load ptr, ptr {handle_ptr}")
            text = self._emit_expr(data)
            n = self._fresh_tmp()
            self._emit_line(f"{n} = call i64 @strlen(ptr {text})")
            wrote = self._fresh_tmp()
            self._emit_line(f"{wrote} = call i64 @fwrite(ptr {text}, i64 1, i64 {n}, ptr {handle})")
            return self._emit_io_ok("write", wrote) or wrote

        return self._emit_fileio_closed_guard(
            ch, "fw", "write", "write: channel is closed", emit_ok
        )

    def _emit_fileio_read(self, args: list[N.Expr]) -> str | None:
        if len(args) != 1:
            return None
        (ch,) = args

        def emit_ok(handle_ptr: str) -> str:
            self._declare_extern("declare i32 @fseek(ptr, i64, i32)")
            self._declare_extern("declare i64 @ftell(ptr)")
            self._declare_extern("declare void @rewind(ptr)")
            self._declare_extern("declare i64 @fread(ptr, i64, i64, ptr)")
            self._declare_extern("declare ptr @malloc(i64)")
            handle = self._fresh_tmp()
            self._emit_line(f"{handle} = load ptr, ptr {handle_ptr}")
            self._emit_line(f"call i32 @fseek(ptr {handle}, i64 0, i32 2)")
            size = self._fresh_tmp()
            self._emit_line(f"{size} = call i64 @ftell(ptr {handle})")
            self._emit_line(f"call void @rewind(ptr {handle})")
            size_p1 = self._fresh_tmp()
            self._emit_line(f"{size_p1} = add i64 {size}, 1")
            buf = self._fresh_tmp()
            self._emit_line(f"{buf} = call ptr @malloc(i64 {size_p1})")
            self._emit_line(f"call i64 @fread(ptr {buf}, i64 1, i64 {size}, ptr {handle})")
            end = self._fresh_tmp()
            self._emit_line(f"{end} = getelementptr i8, ptr {buf}, i64 {size}")
            self._emit_line(f"store i8 0, ptr {end}")
            return self._emit_io_ok("read", buf) or buf

        return self._emit_fileio_closed_guard(ch, "fr", "read", "read: channel is closed", emit_ok)

    def _emit_fileio_close(self, args: list[N.Expr]) -> str | None:
        if len(args) != 1:
            return None
        (ch,) = args

        def emit_ok(handle_ptr: str) -> str:
            self._declare_extern("declare i32 @fclose(ptr)")
            handle = self._fresh_tmp()
            self._emit_line(f"{handle} = load ptr, ptr {handle_ptr}")
            self._emit_line(f"call i32 @fclose(ptr {handle})")
            closed_ptr = self._emit_fileio_field(ch, 2)
            self._emit_line(f"store i1 1, ptr {closed_ptr}")
            return self._emit_io_ok("close", None) or "null"

        return self._emit_fileio_closed_guard(
            ch, "fc", "close", "close: channel is already closed", emit_ok
        )

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

        fmt_args: list[tuple[str, str, N.Expr]] = []
        for arg in args[1:]:
            val = self._emit_expr(arg)
            if val is None:
                # Used to be skipped, leaving its `{}` placeholder printed
                # literally -- e.g. `(fmt "{}" (std/math/sqrt 4.0))` printed "{}".
                raise NotImplementedError(
                    f"codegen: a `fmt` argument ({arg.span}) produced no value"
                )
            ty = self._infer_llvm_type(arg)
            fmt_args.append((ty, val, arg))

        template_text = None
        if isinstance(args[0], N.StringLit):
            template_text = args[0].value
        elif isinstance(args[0], N.Ident) and args[0].name in self._str_lits:
            template_text = self._str_lits[args[0].name]

        if template_text is not None:
            c_fmt = template_text
            for llvm_ty, _, arg_node in fmt_args:
                # `%d` only matches a 32-bit vararg -- an i64 argument
                # (passed at its real width just below, in the
                # `snprintf_args` loop) read back through `%d` silently
                # truncates to its low 32 bits instead of erroring,
                # since C varargs have no type checking. `_emit_out`
                # already gets this right per-argument; `fmt` here
                # needs the same i64 case, not just ptr/float/else.
                # Likewise an unsigned Nyet type (u32/u64/usize) needs
                # `%u`/`%llu`, not `%d`/`%lld` -- see `_env_unsigned_names`.
                unsigned = self._node_is_unsigned(arg_node)
                spec = (
                    "%s"
                    if llvm_ty == "ptr"
                    else "%g"
                    if self._is_float(llvm_ty)
                    else "%llu"
                    if llvm_ty == "i64" and unsigned
                    else "%lld"
                    if llvm_ty == "i64"
                    # Same fix as _emit_out's char case: char is i32-shaped,
                    # so without this it silently printed the numeric
                    # codepoint via %d instead of the character via %c.
                    else "%c"
                    if self._node_is_char(arg_node)
                    else "%u"
                    if unsigned
                    else "%d"
                )
                c_fmt = c_fmt.replace("{}", spec, 1)
            fmt_name = self._get_format_string(c_fmt, f"fmt_{id(args[0])}")
        else:
            fmt_name = template_val

        buf_ptr = self._fresh_tmp()
        self._emit_line(f"{buf_ptr} = call ptr @malloc(i64 1024)")
        self._declare_extern("declare ptr @malloc(i64)")
        snprintf_args = f"ptr {buf_ptr}, i32 1024, ptr {fmt_name}"
        for llvm_ty, val, arg_node in fmt_args:
            if llvm_ty not in ("i64", "double", "float") and self._node_is_unsigned(arg_node):
                val = self._coerce_int_to(val, llvm_ty, "i32", unsigned=True)
                llvm_ty = "i32"
            if llvm_ty == "float":
                # Variadic callee (snprintf %g) expects double — promote
                ext = self._fresh_tmp()
                self._emit_line(f"{ext} = fpext float {val} to double")
                val = ext
                llvm_ty = "double"
            snprintf_args += f", {llvm_ty} {val}"
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call i32 (ptr, i32, ptr, ...) @snprintf({snprintf_args})")
        return buf_ptr

    # ------------------------------------------------------------------
    # Arithmetic (type-aware: int or float)
    # ------------------------------------------------------------------

    @staticmethod
    def _op_mangle(struct_name: str, op: str) -> str:
        """Generate a unique LLVM-safe name for an impl method (operator or named)."""
        op_words = {
            "+": "add",
            "-": "sub",
            "*": "mul",
            "/": "div",
            "%": "mod",
            "==": "eq",
            "!=": "ne",
            "<": "lt",
            "<=": "le",
            ">": "gt",
            ">=": "ge",
            "&&": "and",
            "||": "or",
            "!": "not",
            "()": "call",
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

    def _maybe_dispatch_op_impl(self, op: str, args: list[N.Expr]) -> str | None:
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

    @staticmethod
    def _nest_binary_left(op: str, args: list[N.Expr]) -> N.Call:
        """`(op a b c d)` -> `(op (op (op a b) c) d)`."""
        acc = N.Call(args[0].span, N.Ident(args[0].span, op), [args[0], args[1]])
        for arg in args[2:]:
            acc = N.Call(arg.span, N.Ident(arg.span, op), [acc, arg])
        return acc

    def _emit_arith(self, op: str, args: list[N.Expr]) -> str | None:
        if len(args) < 2:
            return None
        if len(args) > 2:
            # Arithmetic operators are variadic and left-associative, per
            # main.no: `(+ "hello" ", " "world")`, `(* 3.14159 r r)`, and an
            # operator impl applied across several operands like
            # `(/ api "users" "42")` all mean `(op (op a b) c)`. Everything
            # below only ever looks at args[0]/args[1], so every operand
            # past the second used to be silently dropped. Rewriting to
            # nested binary calls reuses the existing type inference and
            # operator-impl dispatch for each step.
            return self._emit_expr(self._nest_binary_left(op, args))
        dispatched = self._maybe_dispatch_op_impl(op, args)
        if dispatched is not None:
            return dispatched
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            return None

        lty = self._infer_llvm_type(args[0])
        rty = self._infer_llvm_type(args[1])

        if op == "+" and lty == "ptr" and rty == "ptr":
            return self._emit_string_concat(lhs, rhs)
        if op == "/" and lty == "ptr" and rty == "ptr":
            # `(/ "usr" "local")` -- main.no's path operator on strings
            # joins with a separator: `a + "/" + b`. Previously fell into
            # the integer path below and emitted `sdiv ptr`, invalid IR.
            sep, _ = self._get_string("/")
            return self._emit_string_concat(self._emit_string_concat(lhs, sep), rhs)

        if self._is_float(lty) or self._is_float(rty):
            # Use double if either operand is double, else float
            fty = "double" if "double" in (lty, rty) else "float"
            if not self._is_float(lty):
                conv = self._fresh_tmp()
                iop = "uitofp" if self._node_is_unsigned(args[0]) else "sitofp"
                self._emit_line(f"{conv} = {iop} {lty} {lhs} to {fty}")
                lhs = conv
            elif lty != fty:
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = fpext float {lhs} to double")
                lhs = conv
            if not self._is_float(rty):
                conv = self._fresh_tmp()
                iop = "uitofp" if self._node_is_unsigned(args[1]) else "sitofp"
                self._emit_line(f"{conv} = {iop} {rty} {rhs} to {fty}")
                rhs = conv
            elif rty != fty:
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = fpext float {rhs} to double")
                rhs = conv
            tmp = self._fresh_tmp()
            fops = {"+": "fadd", "-": "fsub", "*": "fmul", "/": "fdiv", "%": "frem"}
            self._emit_line(f"{tmp} = {fops[op]} {fty} {lhs}, {rhs}")
            return tmp
        else:
            # Integer arithmetic at a common, correctly-widened type --
            # mirrors _emit_cmp's cty/_coerce_int_to handling below, since
            # this path previously hardcoded i32 and miscompiled any
            # arithmetic on i64 operands (e.g. i64 loop counters).
            cty = self._common_cmp_type(args[0], args[1], lty, rty)
            unsigned = self._node_is_unsigned(args[0]) or self._node_is_unsigned(args[1])
            lhs = self._coerce_int_to(lhs, lty, cty, unsigned)
            rhs = self._coerce_int_to(rhs, rty, cty, unsigned)
            tmp = self._fresh_tmp()
            if unsigned:
                iops = {"+": "add", "-": "sub", "*": "mul", "/": "udiv", "%": "urem"}
            else:
                iops = {"+": "add", "-": "sub", "*": "mul", "/": "sdiv", "%": "srem"}
            self._emit_line(f"{tmp} = {iops[op]} {cty} {lhs}, {rhs}")
            return tmp

    def _emit_string_concat(self, lhs: str, rhs: str) -> str:
        """`(+ a b)` on two strings -> malloc + strcpy + strcat.

        Strings are plain null-terminated C strings (ptr), not a
        length-prefixed fat pointer, so libc's string functions apply
        directly.
        """
        self._declare_extern("declare i64 @strlen(ptr)")
        self._declare_extern("declare ptr @malloc(i64)")
        self._declare_extern("declare ptr @strcpy(ptr, ptr)")
        self._declare_extern("declare ptr @strcat(ptr, ptr)")

        la = self._fresh_tmp()
        self._emit_line(f"{la} = call i64 @strlen(ptr {lhs})")
        lb = self._fresh_tmp()
        self._emit_line(f"{lb} = call i64 @strlen(ptr {rhs})")
        total_pre = self._fresh_tmp()
        self._emit_line(f"{total_pre} = add i64 {la}, {lb}")
        total = self._fresh_tmp()
        self._emit_line(f"{total} = add i64 {total_pre}, 1")
        buf = self._fresh_tmp()
        self._emit_line(f"{buf} = call ptr @malloc(i64 {total})")
        copy_tmp = self._fresh_tmp()
        self._emit_line(f"{copy_tmp} = call ptr @strcpy(ptr {buf}, ptr {lhs})")
        cat_tmp = self._fresh_tmp()
        self._emit_line(f"{cat_tmp} = call ptr @strcat(ptr {buf}, ptr {rhs})")
        return buf

    # ------------------------------------------------------------------
    # Bitwise operators — main.no documents `&`/`|`/`^`/`~`/`<<`/`>>`, and
    # the lexer/parser already tokenize them into ordinary operator-named
    # Call nodes (`_OPERATOR_TOKENS` in parser.py), but until this fix
    # nothing in codegen handled them: `(& a b)` (2-arg bitwise AND, as
    # opposed to the 1-arg borrow `&x`) fell through every dispatch case
    # in `_emit_call` and silently evaluated to `None` (dropped output,
    # no error); `|`/`^`/`~`/`<<`/`>>` aren't recognized as borrow syntax
    # at all, so they fell all the way through to `_emit_user_call`,
    # emitting an outright illegal `call i32 @|(...)` (clang: "expected
    # value token") since `|`/`^`/`~`/`<`/`>` aren't valid characters in
    # an LLVM identifier.
    # ------------------------------------------------------------------

    def _emit_bitwise(self, op: str, args: list[N.Expr]) -> str | None:
        if len(args) < 2:
            return None
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            return None
        lty = self._infer_llvm_type(args[0])
        rty = self._infer_llvm_type(args[1])
        cty = self._common_cmp_type(args[0], args[1], lty, rty)
        unsigned = self._node_is_unsigned(args[0]) or self._node_is_unsigned(args[1])
        lhs = self._coerce_int_to(lhs, lty, cty, unsigned)
        rhs = self._coerce_int_to(rhs, rty, cty, unsigned)
        tmp = self._fresh_tmp()
        iops = {"&": "and", "|": "or", "^": "xor"}
        self._emit_line(f"{tmp} = {iops[op]} {cty} {lhs}, {rhs}")
        return tmp

    def _emit_shift(self, op: str, args: list[N.Expr]) -> str | None:
        if len(args) < 2:
            return None
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            return None
        lty = self._infer_llvm_type(args[0])
        rty = self._infer_llvm_type(args[1])
        # LLVM's shift instructions require both operands at the same
        # width -- coerce the shift-amount side to match the shifted
        # value's type rather than the other way around, since the
        # amount is conventionally a small plain int regardless of the
        # shifted value's declared width.
        rhs = self._coerce_int_to(rhs, rty, lty, self._node_is_unsigned(args[1]))
        tmp = self._fresh_tmp()
        if op == "<<":
            instr = "shl"
        else:
            # `>>` is arithmetic (sign-preserving) for a signed source,
            # logical (zero-filling) for an unsigned one -- see
            # `_env_unsigned_names`.
            instr = "lshr" if self._node_is_unsigned(args[0]) else "ashr"
        self._emit_line(f"{tmp} = {instr} {lty} {lhs}, {rhs}")
        return tmp

    def _emit_bitnot(self, arg: N.Expr) -> str | None:
        val = self._emit_expr(arg)
        if val is None:
            return None
        ty = self._infer_llvm_type(arg)
        tmp = self._fresh_tmp()
        # LLVM has no dedicated bitwise-NOT instruction; `xor <val>, -1`
        # is the standard idiom (every bit of -1 is set).
        self._emit_line(f"{tmp} = xor {ty} {val}, -1")
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
                iop = "uitofp" if self._node_is_unsigned(args[0]) else "sitofp"
                self._emit_line(f"{conv} = {iop} {lty} {lhs} to {fty}")
                lhs = conv
            elif lty != fty:
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = fpext float {lhs} to double")
                lhs = conv
            if not self._is_float(rty):
                conv = self._fresh_tmp()
                iop = "uitofp" if self._node_is_unsigned(args[1]) else "sitofp"
                self._emit_line(f"{conv} = {iop} {rty} {rhs} to {fty}")
                rhs = conv
            elif rty != fty:
                conv = self._fresh_tmp()
                self._emit_line(f"{conv} = fpext float {rhs} to double")
                rhs = conv
            tmp = self._fresh_tmp()
            fconds = {"==": "oeq", "!=": "one", "<": "olt", ">": "ogt", "<=": "ole", ">=": "oge"}
            self._emit_line(f"{tmp} = fcmp {fconds[op]} {fty} {lhs}, {rhs}")
            return tmp
        elif (
            lty == "ptr"
            and rty == "ptr"
            and (
                (self._is_string_operand(args[0]) and self._is_string_operand(args[1]))
                # Comparing any pointer to a string literal by address is never
                # meaningful, so a literal on either side means a string compare
                # (e.g. an `(await handle)` result against "expected").
                or isinstance(args[0], N.StringLit)
                or isinstance(args[1], N.StringLit)
            )
        ):
            # `string` lowers to `ptr` same as structs/arrays/maps, but
            # unlike those it's a primitive value type with no `impl Eq`
            # to dispatch through -- so every string `==`/`!=`/`<` used to
            # fall into the raw-pointer-identity branch below, comparing
            # *addresses* instead of contents. Two runtime strings (e.g.
            # a line read via `(in)` against a literal) are essentially
            # never the same allocation, so `(== cmd "quit")` could never
            # match what the user actually typed -- it only ever looked
            # like it worked for two identical string *literals*, which
            # get interned to the same global constant by `_get_string`
            # and were therefore accidentally pointer-equal.
            return self._emit_string_cmp(op, lhs, rhs)
        else:
            # Integer / bool / char / pointer comparison. Choose a common
            # operand type and coerce the narrower side up to it, so that
            # `bool == bool` (i1), `i64 == i64`, and `char == 65` all emit a
            # well-typed `icmp` instead of assuming i32.
            cty = self._common_cmp_type(args[0], args[1], lty, rty)
            unsigned = self._node_is_unsigned(args[0]) or self._node_is_unsigned(args[1])
            lhs = self._coerce_int_to(lhs, lty, cty, unsigned)
            rhs = self._coerce_int_to(rhs, rty, cty, unsigned)
            tmp = self._fresh_tmp()
            if unsigned:
                iconds = {"==": "eq", "!=": "ne", "<": "ult", ">": "ugt", "<=": "ule", ">=": "uge"}
            else:
                iconds = {"==": "eq", "!=": "ne", "<": "slt", ">": "sgt", "<=": "sle", ">=": "sge"}
            self._emit_line(f"{tmp} = icmp {iconds[op]} {cty} {lhs}, {rhs}")
            return tmp

    def _common_cmp_type(self, a: N.Node, b: N.Node, lty: str, rty: str) -> str:
        """Pick the LLVM type to compare two integer-ish operands at.

        A bare integer literal (which always infers as i32) yields to the
        other operand's concrete width; otherwise the wider of the two
        wins. Pointer operands compare as `ptr`.
        """
        if lty == "ptr" or rty == "ptr":
            return "ptr"
        a_lit = isinstance(a, N.IntLit)
        b_lit = isinstance(b, N.IntLit)
        if a_lit and not b_lit:
            return rty
        if b_lit and not a_lit:
            return lty
        return lty if self._sizeof(lty) >= self._sizeof(rty) else rty

    def _coerce_int_to(self, val: str, src: str, dst: str, unsigned: bool = False) -> str:
        """Widen/narrow an integer value from `src` to `dst` for comparison.
        i1 widens via zext (so `true` → 1); other ints widen via zext when
        `unsigned` is set (the source binding has a Nyet u8/u16/u32/u64/
        usize type -- LLVM's plain iN carries no signedness of its own, so
        this is the only place that distinction survives), otherwise
        sext."""
        if src == dst or dst == "ptr" or src == "ptr":
            return val
        sb, db = self._sizeof(src), self._sizeof(dst)
        if sb == db:
            return val
        tmp = self._fresh_tmp()
        if db > sb:
            opc = "zext" if (src == "i1" or unsigned) else "sext"
            self._emit_line(f"{tmp} = {opc} {src} {val} to {dst}")
        else:
            self._emit_line(f"{tmp} = trunc {src} {val} to {dst}")
        return tmp

    def _is_string_operand(self, node: N.Node) -> bool:
        """True if `node` is confidently known to be a Nyet `string`
        (as opposed to some other `ptr`-shaped type -- struct, array,
        tuple, Map -- that also needs comparison to fall through to
        raw pointer identity). Deliberately conservative: covers string
        literals, `string`-typed let/var/param bindings (tracked in
        `_env_string_names`), `fmt` calls, and `string`-typed struct
        fields, which is every shape a string reaches `_emit_cmp` in in
        idiomatic code (bind-then-compare); an inline `(in)`/`(+ ...)`
        concat result not yet bound to a variable falls through to the
        old pointer-identity path rather than risk misclassifying an
        unrelated pointer type.
        """
        if isinstance(node, N.StringLit):
            return True
        if isinstance(node, N.Ident):
            return node.name in self._env_string_names
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            name = node.head.name
            if name == "fmt":
                return True
            if name in ("+", "/") and node.args:
                # String concatenation / path join -- see `_emit_arith`.
                return all(self._is_string_operand(a) for a in node.args)
            # A call whose declared return type is `string`: a method, a
            # plain function, or a generic function.
            if node.args and name.isidentifier():
                method = self._receiver_method(node.args[0], name)
                if method is not None:
                    return self._fn_ret_nyet_names.get(method) == "string"
            if name in self._fn_ret_nyet_names:
                return self._fn_ret_nyet_names[name] == "string"
            if name in self._fn_templates:
                return self._nyet_type_name(self._fn_templates[name].return_type) == "string"
            return False
        if isinstance(node, N.FieldAccess):
            struct_name = self._struct_name_of(node.target)
            if struct_name is not None:
                return node.field_name in self._struct_string_fields.get(struct_name, set())
        return False

    def _emit_string_cmp(self, op: str, lhs: str, rhs: str) -> str:
        """Lower a string comparison to `strcmp`, comparing contents
        instead of the addresses `_emit_cmp`'s default `ptr` path
        would compare. `strcmp`'s sign convention (0 iff equal, `< 0`
        iff lhs sorts first, `> 0` iff rhs does) maps directly onto
        every comparison op, not just `==`/`!=`."""
        self._declare_extern("declare i32 @strcmp(ptr, ptr)")
        cmp_result = self._fresh_tmp()
        self._emit_line(f"{cmp_result} = call i32 @strcmp(ptr {lhs}, ptr {rhs})")
        tmp = self._fresh_tmp()
        iconds = {"==": "eq", "!=": "ne", "<": "slt", ">": "sgt", "<=": "sle", ">=": "sge"}
        self._emit_line(f"{tmp} = icmp {iconds[op]} i32 {cmp_result}, 0")
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
        self._emit_line(f"{tmp} = {'and' if op == '&&' else 'or'} i1 {lhs}, {rhs}")
        return tmp

    # ------------------------------------------------------------------
    # Cast — (as expr type)
    # ------------------------------------------------------------------

    def _emit_cast(self, value: N.Node | None, target_tn: N.TypeNode | None) -> str | None:
        """Emit an explicit primitive cast, including char ↔ int.

        LLVM instruction selection:
          int  → float   : sitofp
          float → int    : fptosi (truncate toward zero)
          float → float  : fpext (widen) / fptrunc (narrow)
          int  → int     : sext (widen) / trunc (narrow)
          char → int     : sext/trunc on i32 value
          int  → char    : coerce to i32 + Unicode bounds-check panic
        """
        src = self._emit_expr(value)
        if src is None:
            return None

        src_ty = self._infer_llvm_type(value)
        dst_ty = self._llvm_type(target_tn)

        target_is_char = (
            isinstance(target_tn, (N.PrimType, N.NamedType)) and target_tn.name == "char"
        )
        source_is_char = self._node_is_char(value)

        if src_ty == dst_ty and not target_is_char and not source_is_char:
            return src  # no-op

        tmp = self._fresh_tmp()

        # float conversions
        if self._is_float(dst_ty) and not self._is_float(src_ty):
            op = "uitofp" if self._node_is_unsigned(value) else "sitofp"
            self._emit_line(f"{tmp} = {op} {src_ty} {src} to {dst_ty}")
            return tmp
        if not self._is_float(dst_ty) and self._is_float(src_ty):
            op = "fptoui" if self._type_is_unsigned(target_tn) else "fptosi"
            self._emit_line(f"{tmp} = {op} {src_ty} {src} to {dst_ty}")
            return tmp
        if self._is_float(dst_ty) and self._is_float(src_ty):
            src_bits = self._sizeof(src_ty) * 8
            dst_bits = self._sizeof(dst_ty) * 8
            if dst_bits > src_bits:
                self._emit_line(f"{tmp} = fpext {src_ty} {src} to {dst_ty}")
            else:
                self._emit_line(f"{tmp} = fptrunc {src_ty} {src} to {dst_ty}")
            return tmp

        # int / char conversions
        src_bits = self._sizeof(src_ty) * 8
        dst_bits = self._sizeof(dst_ty) * 8

        if target_is_char:
            # Coerce source to i32 first
            coerced = src
            if src_bits < 32:
                coerced = self._fresh_tmp()
                self._emit_line(f"{coerced} = sext {src_ty} {src} to i32")
            elif src_bits > 32:
                coerced = self._fresh_tmp()
                self._emit_line(f"{coerced} = trunc {src_ty} {src} to i32")

            # Unicode scalar value bounds check:
            # valid: 0x000000–0xD7FF and 0xE000–0x10FFFF
            self._declare_printf()
            self._declare_extern("declare void @exit(i32)")
            ok_label = self._fresh_label("cast_char_ok")
            surr_label = self._fresh_label("cast_char_surr")
            hi_label = self._fresh_label("cast_char_hi")
            panic_label = self._fresh_label("cast_char_panic")

            lo_ok = self._fresh_tmp()
            self._emit_line(f"{lo_ok} = icmp sge i32 {coerced}, 0")
            self._emit_line(f"br i1 {lo_ok}, label %{surr_label}, label %{panic_label}")

            self._emit_label(surr_label)
            surr_lo = self._fresh_tmp()
            surr_hi = self._fresh_tmp()
            not_surr = self._fresh_tmp()
            self._emit_line(f"{surr_lo} = icmp slt i32 {coerced}, 55296")
            self._emit_line(f"{surr_hi} = icmp sgt i32 {coerced}, 57343")
            self._emit_line(f"{not_surr} = or i1 {surr_lo}, {surr_hi}")
            self._emit_line(f"br i1 {not_surr}, label %{hi_label}, label %{panic_label}")

            self._emit_label(hi_label)
            hi_ok = self._fresh_tmp()
            self._emit_line(f"{hi_ok} = icmp sle i32 {coerced}, 1114111")
            self._emit_line(f"br i1 {hi_ok}, label %{ok_label}, label %{panic_label}")

            self._emit_label(panic_label)
            msg_name, _ = self._get_string(
                "cast error: integer is not a valid Unicode scalar value\n"
            )
            panic_tmp = self._fresh_tmp()
            self._emit_line(f"{panic_tmp} = call i32 (ptr, ...) @printf(ptr {msg_name})")
            self._emit_line("call void @exit(i32 1)")
            self._emit_line("unreachable")

            self._emit_label(ok_label)
            return coerced

        if source_is_char:
            # char → int: i32 value to dst width
            if dst_bits > 32:
                self._emit_line(f"{tmp} = sext i32 {src} to {dst_ty}")
            elif dst_bits < 32:
                self._emit_line(f"{tmp} = trunc i32 {src} to {dst_ty}")
            else:
                return src  # same width
            return tmp

        # plain int → int
        if dst_bits > src_bits:
            opc = "zext" if self._node_is_unsigned(value) else "sext"
            self._emit_line(f"{tmp} = {opc} {src_ty} {src} to {dst_ty}")
        elif dst_bits < src_bits:
            self._emit_line(f"{tmp} = trunc {src_ty} {src} to {dst_ty}")
        else:
            return src
        return tmp

    def _node_is_char(self, node: N.Node | None) -> bool:
        """Return True if node resolves to a char-typed binding."""
        if isinstance(node, N.Ident):
            return node.name in self._env_char_names
        return False

    def _node_is_unsigned(self, node: N.Node | None) -> bool:
        """Return True if node resolves to an unsigned-integer-typed
        binding (u8/u16/u32/u64/usize) -- see `_env_unsigned_names`."""
        if isinstance(node, N.Ident):
            return node.name in self._env_unsigned_names
        # A struct field declared with an unsigned type, e.g. `(. c val)`
        # where `val:u32` -- reuses `_struct_field_nyet` (the same
        # per-field Nyet-type-name registry `_infer_nyet_type_name`'s own
        # FieldAccess case reads) rather than a separate table.
        if isinstance(node, N.FieldAccess):
            outer = self._infer_nyet_type_name(node.target)
            if outer is not None:
                field_ty = self._struct_field_nyet.get(outer, {}).get(node.field_name)
                return field_ty in self._UNSIGNED_NAMES
        return False

    def _type_is_unsigned(self, tn: N.TypeNode | None) -> bool:
        """Return True if `tn` is itself a u8/u16/u32/u64/usize type
        annotation -- for a cast's declared TARGET type, where there is
        no binding/value node to consult `_node_is_unsigned` on."""
        if isinstance(tn, N.RefType):
            return self._type_is_unsigned(tn.inner)
        if isinstance(tn, (N.PrimType, N.NamedType)):
            return tn.name in self._UNSIGNED_NAMES
        return False

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
        """Struct size matching LLVM's natural (non-packed) layout: each
        field is placed at its own alignment (inserting interior padding
        as needed), then the total is rounded up to a multiple of 8."""
        fields = self._structs.get(name, [])
        if not fields:
            return 8
        offset = 0
        for _, ty in fields:
            align = self._alignof(ty)
            offset = (offset + align - 1) & ~(align - 1)
            offset += self._sizeof(ty)
        return (offset + 7) & ~7

    def _field_offsets(self, types: list[str]) -> list[int]:
        """Byte offsets of `types` laid out sequentially with natural
        alignment/padding (mirrors LLVM's struct layout), so a sum type's
        multi-field variant payload doesn't misalign its later fields."""
        offset = 0
        offsets = []
        for ty in types:
            align = self._alignof(ty)
            offset = (offset + align - 1) & ~(align - 1)
            offsets.append(offset)
            offset += self._sizeof(ty)
        return offsets

    def _sum_type_size_bytes(self, name: str) -> int:
        """Size of a sum type = tag + max (padded) payload."""
        variants = self._sum_types.get(name, [])
        max_payload = 0
        for _, types in variants:
            if not types:
                continue
            offsets = self._field_offsets(types)
            payload = offsets[-1] + self._sizeof(types[-1])
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
        field_array_elem = self._struct_field_array_elem.get(name, {})

        # Match args to fields — support both positional and keyword
        vals: dict[str, str] = {}
        positional = 0
        # Struct update: `(Person ..me age:37)`. The parser yields `..` as its
        # own Ident argument followed by the source expression; every field
        # not given explicitly is copied from the source below.
        spread_src: str | None = None
        spread_at = next(
            (i for i, a in enumerate(args) if isinstance(a, N.Ident) and a.name == ".."), None
        )
        if spread_at is not None:
            if spread_at + 1 >= len(args):
                raise NotImplementedError("codegen: `..` in a struct constructor needs a source")
            spread_src = self._emit_expr(args[spread_at + 1])
            args = args[:spread_at] + args[spread_at + 2 :]
        for arg in args:
            if isinstance(arg, N.KeywordArg) and arg.value is not None:
                elem_ty = field_array_elem.get(arg.name)
                if elem_ty is not None:
                    v = self._emit_expr_as_array(arg.value, elem_ty)
                else:
                    v = self._emit_expr(arg.value)
                if v is not None:
                    vals[arg.name] = v
            else:
                fname = fields[positional][0] if positional < len(fields) else None
                elem_ty = field_array_elem.get(fname) if fname is not None else None
                if elem_ty is not None:
                    v = self._emit_expr_as_array(arg, elem_ty)
                else:
                    v = self._emit_expr(arg)
                if v is not None and positional < len(fields):
                    vals[fields[positional][0]] = v
                positional += 1

        if spread_src is not None:
            for i, (fname, ftype) in enumerate(fields):
                if fname not in vals:
                    src_fptr = self._fresh_tmp()
                    self._emit_line(
                        f"{src_fptr} = getelementptr inbounds %{name}, ptr {spread_src}, "
                        f"i32 0, i32 {i}"
                    )
                    loaded = self._fresh_tmp()
                    self._emit_line(f"{loaded} = load {ftype}, ptr {src_fptr}")
                    vals[fname] = loaded

        for i, (fname, ftype) in enumerate(fields):
            if fname in vals:
                fptr = self._fresh_tmp()
                self._emit_line(
                    f"{fptr} = getelementptr inbounds %{name}, ptr {ptr}, i32 0, i32 {i}"
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

        values: list[tuple[str, str]] = []
        for i, arg in enumerate(args):
            if i >= len(payload_types):
                break
            val = self._emit_expr(arg)
            if val is not None:
                values.append((payload_types[i], val))

        return self._emit_variant_construct_raw(sum_name, tag_idx, payload_types, values)

    def _emit_variant_construct_raw(
        self,
        sum_name: str,
        tag_idx: int,
        payload_types: tuple[str, ...],
        values: list[tuple[str, str]],
    ) -> str:
        """Core of `_emit_variant_construct`, taking already-computed
        (llvm_type, value) pairs instead of AST argument nodes — used
        directly by builtins (`FileIO`/`StdIO`'s `write`/`read`/`close`)
        that construct `Ok`/`Err` values without going through ordinary
        Nyet-source `(Ok ...)`/`(Err ...)` call syntax."""
        ptr = self._heap_alloc_struct(sum_name, self._sum_type_size_bytes(sum_name))

        # Store tag
        tag_ptr = self._fresh_tmp()
        self._emit_line(f"{tag_ptr} = getelementptr inbounds %{sum_name}, ptr {ptr}, i32 0, i32 0")
        self._emit_line(f"store i32 {tag_idx}, ptr {tag_ptr}")

        # Store payload fields
        if payload_types:
            payload_ptr = self._fresh_tmp()
            self._emit_line(
                f"{payload_ptr} = getelementptr inbounds %{sum_name}, ptr {ptr}, i32 0, i32 1"
            )
            offsets = self._field_offsets(payload_types)
            for i, (ftype, val) in enumerate(values):
                off = offsets[i]
                if off == 0:
                    field_ptr = payload_ptr
                else:
                    field_ptr = self._fresh_tmp()
                    self._emit_line(f"{field_ptr} = getelementptr i8, ptr {payload_ptr}, i32 {off}")
                self._emit_line(f"store {ftype} {val}, ptr {field_ptr}")

        return ptr

    # ------------------------------------------------------------------
    # Field access
    # ------------------------------------------------------------------

    def _emit_field_ptr(self, node: N.FieldAccess) -> tuple[str, str] | None:
        """Compute the address of a struct field (GEP only, no load)."""
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
                return fptr, ftype
        return None

    def _emit_field_access(self, node: N.FieldAccess) -> str | None:
        """Emit `.field target` → GEP + load."""
        ptr_and_ty = self._emit_field_ptr(node)
        if ptr_and_ty is None:
            return None
        fptr, ftype = ptr_and_ty
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {ftype}, ptr {fptr}")
        return result

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
        result_ty = self._result_slot_type(result_ty)
        result_ptr = self._emit_alloca(result_ty)

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
                    self._emit_line(f"br i1 {guard_val}, label %{body_label}, label %{next_label}")
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
            ctor = self._variant_ctors.get(composite) or self._variant_ctors.get(pat.name)
            if ctor is None:
                self._emit_line(f"br label %{fail_label}")
                dead = self._fresh_label("after_dead")
                self._emit_label(dead)
                return
            ctor_sum, tag_idx = ctor
            actual_sum = ctor_sum if ctor_sum in self._sum_types else sum_name

            tag_ptr = self._fresh_tmp()
            self._emit_line(
                f"{tag_ptr} = getelementptr inbounds %{actual_sum}, ptr {scrut_val}, i32 0, i32 0"
            )
            tag = self._fresh_tmp()
            self._emit_line(f"{tag} = load i32, ptr {tag_ptr}")
            cmp = self._fresh_tmp()
            self._emit_line(f"{cmp} = icmp eq i32 {tag}, {tag_idx}")
            ok_label = self._fresh_label("tag_ok")
            self._emit_line(f"br i1 {cmp}, label %{ok_label}, label %{fail_label}")
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
                offsets = self._field_offsets(payload_types)
                for pi, ppat in enumerate(pat.args):
                    if pi >= len(payload_types):
                        break
                    pty = payload_types[pi]
                    inner_nyet = nyet_names[pi] if pi < len(nyet_names) else None
                    off = offsets[pi]
                    if off == 0:
                        fld_ptr = payload_ptr
                    else:
                        fld_ptr = self._fresh_tmp()
                        self._emit_line(
                            f"{fld_ptr} = getelementptr i8, ptr {payload_ptr}, i32 {off}"
                        )
                    val = self._fresh_tmp()
                    self._emit_line(f"{val} = load {pty}, ptr {fld_ptr}")
                    self._emit_pattern_test_value(val, pty, inner_nyet, ppat, fail_label)
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
            # A LitPat's own literal node tells us unambiguously whether
            # this is a string comparison (unlike `llvm_ty`, which is
            # just "ptr" for strings/structs/arrays/Maps alike) — no
            # need for `_is_string_operand`'s more conservative,
            # AST-shape-based guess.
            if isinstance(pat.value, N.StringLit):
                cmp = self._emit_string_cmp("==", val, cmp_val)
            else:
                cmp = self._fresh_tmp()
                if llvm_ty in ("double", "float"):
                    self._emit_line(f"{cmp} = fcmp oeq {llvm_ty} {val}, {cmp_val}")
                else:
                    self._emit_line(f"{cmp} = icmp eq {llvm_ty} {val}, {cmp_val}")
            ok_label = self._fresh_label("lit_ok")
            self._emit_line(f"br i1 {cmp}, label %{ok_label}, label %{fail_label}")
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

    def _emit_match_simple(self, scrut: str, node: N.Match) -> str | None:
        """Simple value-based match — integers, floats, bools, and
        strings. `scrut_ty`/`scrut_is_string` come from the scrutinee
        expression itself (previously hardcoded to `i32` unconditionally,
        which broke every non-int scrutinee: a `string` match emitted
        `icmp eq i32` against a `ptr` value — a straight type mismatch —
        and even where the width happened to still verify, e.g. `bool`,
        it was comparing the wrong bit width by luck rather than by
        design)."""
        end_label = self._fresh_label("match_end")
        result_ty = self._infer_llvm_type(node.arms[0].body) if node.arms else "i32"
        result_ty = self._result_slot_type(result_ty)
        result_ptr = self._emit_alloca(result_ty)

        scrut_ty = self._infer_llvm_type(node.scrutinee)
        scrut_is_string = self._is_string_operand(node.scrutinee)

        next_label = self._fresh_label("match_next")
        for i, arm in enumerate(node.arms):
            is_last = i == len(node.arms) - 1
            pat = arm.pattern

            if isinstance(pat, N.WildPat) or isinstance(pat, N.VarPat):
                # Default arm
                if isinstance(pat, N.VarPat):
                    saved = dict(self._env)
                    vptr = self._emit_alloca(scrut_ty)
                    self._emit_line(f"store {scrut_ty} {scrut}, ptr {vptr}")
                    self._env[pat.name] = (vptr, scrut_ty)
                    if scrut_is_string:
                        self._env_string_names.add(pat.name)

                body_val = self._emit_expr(arm.body)
                if body_val is not None:
                    self._emit_line(f"store {result_ty} {body_val}, ptr {result_ptr}")
                self._emit_line(f"br label %{end_label}")

                if isinstance(pat, N.VarPat):
                    self._env = saved
                break

            elif isinstance(pat, N.LitPat):
                cmp_val = self._emit_expr(pat.value)
                if scrut_is_string:
                    cmp = self._emit_string_cmp("==", scrut, cmp_val)
                else:
                    cmp = self._fresh_tmp()
                    if scrut_ty in ("double", "float"):
                        self._emit_line(f"{cmp} = fcmp oeq {scrut_ty} {scrut}, {cmp_val}")
                    else:
                        self._emit_line(f"{cmp} = icmp eq {scrut_ty} {scrut}, {cmp_val}")

                arm_label = self._fresh_label("match_arm")
                if is_last:
                    next_label = self._fresh_label("match_default")
                else:
                    next_label = self._fresh_label("match_next")

                self._emit_line(f"br i1 {cmp}, label %{arm_label}, label %{next_label}")

                self._emit_label(arm_label)
                body_val = self._emit_expr(arm.body)
                if body_val is not None:
                    self._emit_line(f"store {result_ty} {body_val}, ptr {result_ptr}")
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

    def _emit_spawn(self, node: N.Spawn) -> str | None:
        """`(spawn (fn_name arg))` -- runs fn_name(arg) on a new OS
        thread (runtime/async.c's nyet_spawn), returning an opaque
        Handle pointer. Real concurrency, not a stackless coroutine --
        see CONTINUATION_PLAN.md's typed-IR-layer decision for why.

        Scoped to a single call whose target function's one parameter
        and return value are both `ptr` (string/struct/sum-type/array/
        tuple -- i.e. everything except bare scalars): that signature
        already matches pthread's `void *(*)(void *)` start-routine
        exactly, so the target function runs directly as the thread
        body with no trampoline needed on either side.
        """
        call = node.value
        if not (
            isinstance(call, N.Call) and isinstance(call.head, N.Ident) and len(call.args) == 1
        ):
            return None
        fn_name = call.head.name
        if fn_name not in self._fn_sigs:
            return None
        param_tys, ret_ty = self._fn_sigs[fn_name]
        if len(param_tys) != 1 or param_tys[0] != "ptr" or ret_ty != "ptr":
            return None
        arg_val = self._emit_expr(call.args[0])
        if arg_val is None:
            return None
        self._declare_extern("declare ptr @nyet_spawn(ptr, ptr)")
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call ptr @nyet_spawn(ptr @{fn_name}, ptr {arg_val})")
        return tmp

    def _emit_await(self, node: N.Await) -> str | None:
        """`(await handle)` -- joins the spawned thread (nyet_await)
        and returns its result, which arrives via pthread_join's own
        retval mechanism (see runtime/async.c)."""
        handle_val = self._emit_expr(node.value)
        if handle_val is None:
            return None
        self._declare_extern("declare ptr @nyet_await(ptr)")
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call ptr @nyet_await(ptr {handle_val})")
        return tmp

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
            f"{tag_ptr} = getelementptr inbounds %{sum_name}, ptr {scrut_val}, i32 0, i32 0"
        )
        tag = self._fresh_tmp()
        self._emit_line(f"{tag} = load i32, ptr {tag_ptr}")

        # success if tag == 0
        is_ok = self._fresh_tmp()
        self._emit_line(f"{is_ok} = icmp eq i32 {tag}, 0")

        ok_label = self._fresh_label("try_ok")
        fail_label = self._fresh_label("try_fail")
        self._emit_line(f"br i1 {is_ok}, label %{ok_label}, label %{fail_label}")

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
            f"{payload_ptr} = getelementptr inbounds %{sum_name}, ptr {scrut_val}, i32 0, i32 1"
        )
        first_type = payload_types[0]
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {first_type}, ptr {payload_ptr}")
        return result

    def _sum_name_of(self, node: N.Node | None) -> str | None:
        """Return the (possibly mangled) sum type name for a scrutinee."""
        if isinstance(node, N.Ident):
            return self._env_struct_name.get(node.name)
        if self._is_parse_call(node):
            return self._parse_result_type()
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

    def _result_slot_type(self, inferred_ty: str) -> str:
        """Sanitize an `_infer_llvm_type` result before it's used to size
        an `alloca` for an `if`/`match` result slot.

        A branch or arm that's a bare call to a `-> unit` function (not
        wrapped in a `do` alongside other statements) makes
        `_infer_llvm_type` return that callee's real LLVM return type,
        "void" — which is only legal as a function's own result type,
        never as a local/alloca type. `_emit_expr` never actually
        produces an SSA value for such a call anyway (every caller
        guards its store with `if val is not None`), so the slot's type
        only has to be *some* valid one; `ptr` is what an untyped/
        no-value branch already defaults to when there's nothing to
        infer from at all (e.g. `if` with no `then_branch`).
        """
        return "ptr" if inferred_ty == "void" else inferred_ty

    def _emit_if(self, node: N.If) -> str | None:
        cond = self._emit_expr(node.cond)
        if cond is None:
            return None

        then_label = self._fresh_label("then")
        else_label = self._fresh_label("else")
        end_label = self._fresh_label("ifend")

        result_ty = self._infer_llvm_type(node.then_branch) if node.then_branch else "ptr"
        result_ty = self._result_slot_type(result_ty)
        result_ptr = self._emit_alloca(result_ty)
        # Zero-initialize
        if result_ty == "ptr":
            self._emit_line(f"store ptr null, ptr {result_ptr}")
        elif self._is_float(result_ty):
            self._emit_line(f"store {result_ty} 0.0, ptr {result_ptr}")
        else:
            self._emit_line(f"store {result_ty} 0, ptr {result_ptr}")

        self._emit_line(f"br i1 {cond}, label %{then_label}, label %{else_label}")

        self._emit_label(then_label)
        then_val = self._emit_expr(node.then_branch)
        if then_val is not None:
            self._emit_line(f"store {result_ty} {then_val}, ptr {result_ptr}")
        self._emit_line(f"br label %{end_label}")

        self._emit_label(else_label)
        if node.else_branch is not None:
            else_val = self._emit_expr(node.else_branch)
            if else_val is not None:
                self._emit_line(f"store {result_ty} {else_val}, ptr {result_ptr}")
        self._emit_line(f"br label %{end_label}")

        self._emit_label(end_label)
        if then_val is not None:
            result = self._fresh_tmp()
            self._emit_line(f"{result} = load {result_ty}, ptr {result_ptr}")
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

        self._emit_line(f"br label %{top_label}")
        self._emit_label(top_label)

        # [end_label, result_ptr, result_ty]. The result slot is created by the
        # first `(break value)` -- see `_emit_break` -- since only there is the
        # value's type known with the right names in scope, e.g. a match arm's
        # pattern bindings. Sizing it before emitting the body used to see an
        # unrelated outer binding of the same name and pick the wrong type.
        frame: list = [end_label, None, None]
        self._loop_stack.append(frame)
        if node.body is not None:
            self._emit_expr(node.body)
        self._loop_stack.pop()

        # Fall-through back to loop top (unreachable if body always breaks)
        self._emit_line(f"br label %{top_label}")
        self._emit_label(end_label)

        _, result_ptr, break_ty = frame
        if result_ptr is not None and break_ty is not None:
            result = self._fresh_tmp()
            self._emit_line(f"{result} = load {break_ty}, ptr {result_ptr}")
            return result
        return None

    def _emit_break(self, node: N.Break) -> str | None:
        if self._loop_stack:
            frame = self._loop_stack[-1]
            end_label = frame[0]
            if node.value is not None:
                val = self._emit_expr(node.value)
                if val is not None:
                    val_ty = self._infer_llvm_type(node.value)
                    if frame[1] is None:
                        frame[2] = self._result_slot_type(val_ty)
                        frame[1] = self._emit_alloca(frame[2])
                    result_ptr, break_ty = frame[1], frame[2]
                    if self._is_float(break_ty) and not self._is_float(val_ty):
                        conv = self._fresh_tmp()
                        iop = "uitofp" if self._node_is_unsigned(node.value) else "sitofp"
                        self._emit_line(f"{conv} = {iop} {val_ty} {val} to {break_ty}")
                        val = conv
                    self._emit_line(f"store {break_ty} {val}, ptr {result_ptr}")
            self._emit_line(f"br label %{end_label}")
        # Start an unreachable block so any code emitted by callers
        # (e.g. the if-then fallthrough br) remains structurally valid.
        dead = self._fresh_label("dead")
        self._emit_label(dead)
        return None

    def _emit_let(self, node: N.LetDecl | N.ConstDecl) -> str | None:
        if node.type:
            ty = self._llvm_type(node.type)
            nyet_name = self._nyet_type_name(node.type)
        elif node.value:
            # An unannotated `(let r (some_unit_fn))` -- e.g. a macro
            # like `time_it` that binds a caller-supplied body's result
            # regardless of its type -- makes this the same "void isn't
            # a valid local type" trap `_emit_if`/`_emit_match` had:
            # `_infer_llvm_type` reports the callee's real return type,
            # "void" for a bare call to a `-> unit` fn, which `alloca`
            # rejects outright. Route through the same sanitizer.
            ty = self._result_slot_type(self._infer_llvm_type(node.value))
            nyet_name = self._infer_nyet_type_name(node.value)
        else:
            ty = "i32"
            nyet_name = None

        if ty == "void":
            # `(let i:unit ())`: a unit value has no runtime representation, so
            # there is nothing to store -- just evaluate the initializer.
            if node.value is not None:
                self._emit_expr(node.value)
            return None

        # Array bindings: store the heap pointer and remember its element
        # type so `(name i)` and `(= (name i) v)` lower correctly. We
        # detect via either a `Array[T]` annotation (needed when the rhs
        # isn't a literal, e.g. a HOF call like `map`/`filter`) or an
        # `ArrayLit` rhs.
        elem_ty: str | None = None
        if node.type is not None:
            elem_ty = self._array_elem_llvm_type(node.type)
        if elem_ty is None and isinstance(node.value, N.ArrayLit) and node.value.elements:
            elem_ty = self._infer_llvm_type(node.value.elements[0])
        # `(let row (grid i))`, no annotation -- `grid` is itself an
        # Array[Array[U]] binding, so its element (an Array[U]) needs
        # the same array-ness threaded onto `row`. See
        # `_env_array_elem_of_array`.
        elem_of_array_src: str | None = None
        if (
            elem_ty is None
            and isinstance(node.value, N.Call)
            and isinstance(node.value.head, N.Ident)
            and node.value.head.name in self._env_array_elem_of_array
            and len(node.value.args) == 1
        ):
            elem_of_array_src = node.value.head.name
            elem_ty = self._env_array_elem_of_array[elem_of_array_src]
        if elem_ty is None and isinstance(node.value, N.Call):
            # `(let xs (map f nums))` / `(let xs (make_list))` with no
            # annotation: a HOF, or a function declared `-> Array[T]`.
            elem_ty = self._array_expr_elem_ty(node.value)
        if elem_ty is not None:
            elem_nyet: str | None = None
            if node.type is not None:
                elem_nyet = self._array_elem_nyet_name(node.type)
            if elem_nyet is None and isinstance(node.value, N.ArrayLit) and node.value.elements:
                elem_nyet = self._infer_nyet_type_name(node.value.elements[0])
            elem_fn_sig: tuple[list[str], str] | None = None
            if isinstance(node.value, N.ArrayLit) and node.value.elements:
                elem_fn_sig = self._fn_sig_of_value(node.value.elements[0])
            # `elem_of_array` is only set from an explicit nested
            # `Array[Array[U]]` annotation -- NOT inherited from
            # `elem_of_array_src` above, which would incorrectly mark a
            # plain `Array[U]` binding (`row`, one level in) as itself
            # holding arrays (U's own element type), corrupting any
            # further indexing into `row`'s own results.
            elem_of_array: str | None = None
            if node.type is not None:
                elem_of_array = self._array_of_array_elem_llvm_type(node.type)
            ptr = self._emit_alloca("ptr")
            val = self._emit_expr_as_array(node.value, elem_ty) if node.value is not None else None
            if val is not None:
                self._emit_line(f"store ptr {val}, ptr {ptr}")
            else:
                self._emit_line(f"store ptr null, ptr {ptr}")
            self._env[node.name] = (ptr, "ptr")
            self._env_array_elem[node.name] = elem_ty
            if elem_nyet is not None:
                self._env_array_elem_nyet[node.name] = elem_nyet
            if elem_fn_sig is not None:
                self._env_array_elem_fn_sig[node.name] = elem_fn_sig
            if elem_of_array is not None:
                self._env_array_elem_of_array[node.name] = elem_of_array
            return None

        # Tuple bindings: store the heap pointer and remember the
        # synthesized tuple struct type so `(name i)` lowers correctly.
        # Detected via an explicit `#(T1 T2)` annotation, a literal
        # `TupleLit` rhs, or a call to a fn registered in
        # `_fn_ret_tuple_types` (needed when neither of the above holds,
        # e.g. `(let p (make_pair))` with no annotation).
        tuple_elem_tys: list[str] | None = None
        tuple_elem_nyet: list[str | None] | None = None
        tname: str | None = None
        if isinstance(node.type, N.TupleType):
            tuple_elem_tys = [self._llvm_type(et) for et in node.type.elements]
            tuple_elem_nyet = [self._nyet_type_name(et) for et in node.type.elements]
        elif isinstance(node.value, N.TupleLit):
            tuple_elem_tys = [self._infer_llvm_type(e) for e in node.value.elements]
            tuple_elem_nyet = [self._infer_nyet_type_name(e) for e in node.value.elements]
        elif (
            isinstance(node.value, N.Call)
            and isinstance(node.value.head, N.Ident)
            and node.value.head.name in self._fn_ret_tuple_types
        ):
            tname = self._fn_ret_tuple_types[node.value.head.name]
        elif (
            isinstance(node.value, N.Call)
            and isinstance(node.value.head, N.Ident)
            and node.value.head.name in self._fn_templates
        ):
            # A generic function returning a tuple: `(let s (swap #(1 "x")))`.
            mangled = self._monomorphize_fn_from_args(node.value.head.name, node.value.args)
            if mangled is not None:
                tname = self._fn_ret_tuple_types.get(mangled)
        elif (
            isinstance(node.value, N.Call)
            and isinstance(node.value.head, N.Ident)
            and node.value.head.name in self._env_tuple_types
            and len(node.value.args) == 1
            and isinstance(node.value.args[0], N.IntLit)
        ):
            # `(let inner (outer 1))` where element 1 is itself a tuple, as a
            # nested destructuring `(let #(a #(b c)) ...)` produces.
            tname = self._tuple_field_tuple.get(
                (self._env_tuple_types[node.value.head.name], node.value.args[0].value)
            )
        if tuple_elem_tys is not None or tname is not None:
            if tname is None:
                tname = self._get_or_register_tuple_type(tuple_elem_tys, tuple_elem_nyet)
                self._record_nested_tuple_fields(tname, node)
            ptr = self._emit_alloca("ptr")
            if node.value is not None:
                val = self._emit_expr(node.value)
                if val is not None:
                    self._emit_line(f"store ptr {val}, ptr {ptr}")
                else:
                    self._emit_line(f"store ptr null, ptr {ptr}")
            else:
                self._emit_line(f"store ptr null, ptr {ptr}")
            self._env[node.name] = (ptr, "ptr")
            self._env_tuple_types[node.name] = tname
            return None

        # Map[string V] bindings: store the heap pointer and remember the
        # value type so `(name key)` / `(= (name key) v)` lower correctly.
        # Detected via either a `Map[K V]` annotation or a literal
        # `MapLit` rhs (value type inferred from its first entry).
        map_val_ty: str | None = None
        if node.type is not None:
            map_val_ty = self._map_val_llvm_type(node.type)
        if map_val_ty is None and isinstance(node.value, N.MapLit) and node.value.entries:
            map_val_ty = self._infer_llvm_type(node.value.entries[0][1])
        if map_val_ty is not None:
            map_val_nyet: str | None = None
            if node.type is not None:
                map_val_nyet = self._map_val_nyet_name(node.type)
            if map_val_nyet is None and isinstance(node.value, N.MapLit) and node.value.entries:
                map_val_nyet = self._infer_nyet_type_name(node.value.entries[0][1])
            map_val_fn_sig: tuple[list[str], str] | None = None
            if isinstance(node.value, N.MapLit) and node.value.entries:
                map_val_fn_sig = self._fn_sig_of_value(node.value.entries[0][1])
            ptr = self._emit_alloca("ptr")
            val = self._emit_expr(node.value) if node.value is not None else None
            if val is not None:
                self._emit_line(f"store ptr {val}, ptr {ptr}")
            else:
                self._emit_line(f"store ptr null, ptr {ptr}")
            self._env[node.name] = (ptr, "ptr")
            self._env_map_val_ty[node.name] = map_val_ty
            if map_val_nyet is not None:
                self._env_map_val_nyet[node.name] = map_val_nyet
            if map_val_fn_sig is not None:
                self._env_map_val_fn_sig[node.name] = map_val_fn_sig
            return None

        # For struct/sum-type bindings, the value is already a ptr (from construction)
        is_aggregate = nyet_name is not None and (
            nyet_name in self._structs or nyet_name in self._sum_types
        )

        if is_aggregate:
            # Value is a ptr to the struct — store the ptr itself
            if node.value is not None:
                # Thread this `let`'s own annotation through as a
                # fallback type-arg source for a generic sum-type
                # variant constructor whose own field types can't fully
                # resolve every generic param (e.g. `(let r:Result[i32
                # string] (Ok v))`, where `Ok`'s field never mentions
                # `string`) -- see `_resolve_generic_variant`.
                saved_ret_type_node = self._current_fn_return_type_node
                if node.type is not None:
                    self._current_fn_return_type_node = node.type
                val = self._emit_expr(node.value)
                self._current_fn_return_type_node = saved_ret_type_node
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
                    if ty == "float" and isinstance(node.value, N.FloatLit):
                        # `(let e:f32 3.14)`: a double constant is only a valid
                        # `float` constant if it's exactly representable, so
                        # round the literal to f32 first.
                        val = self._float_const(node.value.value, "float")
                    elif self._is_float(ty) and self._is_float(val_ty) and val_ty != ty:
                        conv = self._fresh_tmp()
                        fop = "fptrunc" if ty == "float" else "fpext"
                        self._emit_line(f"{conv} = {fop} {val_ty} {val} to {ty}")
                        val = conv
                    elif self._is_float(ty) and not self._is_float(val_ty):
                        conv = self._fresh_tmp()
                        iop = "uitofp" if self._node_is_unsigned(node.value) else "sitofp"
                        self._emit_line(f"{conv} = {iop} {val_ty} {val} to {ty}")
                        val = conv
                    elif not self._is_float(ty) and not self._is_float(val_ty) and val_ty != ty:
                        # Integer width mismatch — coerce to match declared type
                        src_bits = self._sizeof(val_ty) * 8
                        dst_bits = self._sizeof(ty) * 8
                        conv = self._fresh_tmp()
                        if dst_bits > src_bits:
                            opc = "zext" if self._node_is_unsigned(node.value) else "sext"
                            self._emit_line(f"{conv} = {opc} {val_ty} {val} to {ty}")
                        else:
                            self._emit_line(f"{conv} = trunc {val_ty} {val} to {ty}")
                        val = conv
                    self._emit_line(f"store {ty} {val}, ptr {ptr}")
            self._env[node.name] = (ptr, ty)
            # Track char bindings so _emit_cast can detect char→int conversions.
            if node.type is not None and self._nyet_type_name(node.type) == "char":
                self._env_char_names.add(node.name)
            # Track unsigned-typed bindings -- see `_env_unsigned_names`.
            if node.type is not None and self._nyet_type_name(node.type) in self._UNSIGNED_NAMES:
                self._env_unsigned_names.add(node.name)
            # Track string bindings so `(name i)` lowers to a byte index. A
            # `:string` annotation is authoritative; otherwise a bare string
            # literal RHS (`(let z "hi")`) infers the same shape.
            declared_string = node.type is not None and self._nyet_type_name(node.type) == "string"
            # Unannotated: a literal, or anything `_is_string_operand` knows is
            # a string (`(let msg (fmt ...))`, a call returning `string`).
            inferred_string = (
                node.type is None and node.value is not None and self._is_string_operand(node.value)
            )
            if declared_string or inferred_string:
                self._env_string_names.add(node.name)
            elif node.name in self._env_string_names:
                # A rebinding to a non-string shadows an earlier string of the
                # same name — drop the stale entry so `(name i)` isn't misread.
                self._env_string_names.discard(node.name)

        if isinstance(node.value, N.StringLit):
            self._str_lits[node.name] = node.value.value

        # v0.6: if the let binds to a function (lifted lambda or top-level
        # fn name), copy the signature over so `(<name> args...)` later
        # dispatches as an indirect call through the slot.
        sig = self._fn_sig_of_value(node.value)
        if sig is not None:
            self._env_fn_sig[node.name] = sig
        return None

    def _fn_sig_of_value(self, value: N.Node | None) -> tuple[list[str], str] | None:
        """If `value` is an Ident referring to a known function, return its
        (param_llvm_types, ret_llvm_type) signature. Otherwise None."""
        if value is None:
            return None
        if isinstance(value, N.Ident):
            if value.name in self._fn_sigs:
                return self._fn_sigs[value.name]
            if value.name in self._env_fn_sig:
                return self._env_fn_sig[value.name]
        # A call to a function that returns a function: `(let add5 (make_adder 5))`.
        if (
            isinstance(value, N.Call)
            and isinstance(value.head, N.Ident)
            and value.head.name in self._fn_ret_fn_sig
        ):
            return self._fn_ret_fn_sig[value.head.name]
        # A Map[K V] lookup `(m key)` where `m`'s values are themselves
        # closures/fn pointers (a dispatch-table pattern) -- see
        # `_env_map_val_fn_sig`.
        if (
            isinstance(value, N.Call)
            and isinstance(value.head, N.Ident)
            and value.head.name in self._env_map_val_fn_sig
            and len(value.args) == 1
            and value.head.name in self._env_map_val_ty
        ):
            return self._env_map_val_fn_sig[value.head.name]
        # Same shape, for an Array[T] of closures/fn pointers.
        if (
            isinstance(value, N.Call)
            and isinstance(value.head, N.Ident)
            and value.head.name in self._env_array_elem_fn_sig
            and len(value.args) == 1
            and value.head.name in self._env_array_elem
        ):
            return self._env_array_elem_fn_sig[value.head.name]
        return None

    def _infer_nyet_type_name(self, node: N.Node) -> str | None:
        """Try to infer the Nyet type name from an expression."""
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            name = node.head.name
            # Array indexing `(arr i)` where `arr` holds struct/sum-type
            # elements — a bound name shadows any same-named function,
            # matching `_emit_call`'s own array-indexing precedence.
            if (
                name in self._env_array_elem_nyet
                and len(node.args) == 1
                and name in self._env_array_elem
            ):
                return self._env_array_elem_nyet[name]
            # Same shape, for a Map[K V] lookup `(m key)`.
            if (
                name in self._env_map_val_nyet
                and len(node.args) == 1
                and name in self._env_map_val_ty
            ):
                return self._env_map_val_nyet[name]
            # Tuple indexing `(t i)` where element `i` is itself a
            # struct/sum type and `i` is a literal (the only case a
            # specific field can be resolved at all, same restriction
            # `_infer_llvm_type` already has for this shape).
            if (
                name in self._env_tuple_types
                and len(node.args) == 1
                and isinstance(node.args[0], N.IntLit)
            ):
                tname = self._env_tuple_types[name]
                return self._struct_field_nyet.get(tname, {}).get(str(node.args[0].value))
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
            # Generic fn — mirrors `_infer_llvm_type`'s matching case.
            # `_fn_ret_nyet_names[name]` above only ever holds a
            # *template's* own (unsubstituted, generic-param-shaped)
            # return type name, never a specific instantiation's, so a
            # generic-returning call bound with no annotation
            # (`(let p (make_pair 5 "hello"))`) needs its own type args
            # inferred from this call's own arguments.
            if name in self._fn_templates:
                tmpl = self._fn_templates[name]
                type_args = self._infer_type_args_for_fn(tmpl, node.args)
                if type_args is not None:
                    mangled = self._mangle(name, type_args)
                    if mangled in self._fn_ret_nyet_names:
                        return self._fn_ret_nyet_names[mangled]
                    env: dict[str, N.TypeNode] = {}
                    for gp, targ in zip(tmpl.generics, type_args, strict=False):
                        env[gp.name] = N.NamedType(tmpl.span, targ)
                    rt = self._subst_type(tmpl.return_type, env)
                    return self._nyet_type_name(rt)
        if (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Path)
            and len(node.head.segments) == 2
        ):
            # `(Type/name ...)` — see `_emit_call`'s matching case.
            mangled = self._method_impls.get(tuple(node.head.segments))
            if mangled is not None:
                return self._fn_ret_nyet_names.get(mangled)
        if isinstance(node, N.Call) and (unq := self._unqualify_module_call(node)) is not None:
            return self._infer_nyet_type_name(unq)
        if self._is_parse_call(node):
            return self._parse_result_type()
        if isinstance(node, N.Ident) and node.name in self._env_struct_name:
            return self._env_struct_name[node.name]
        # A struct field that itself holds a struct/sum-type value —
        # `(. o inner)`, chained onto another field access or bound
        # with no `&T` annotation.
        if isinstance(node, N.FieldAccess):
            outer = self._infer_nyet_type_name(node.target)
            if outer is not None:
                return self._struct_field_nyet.get(outer, {}).get(node.field_name)
        # `if`/`match`/`do` yield the type of their tail expression —
        # mirrors `_infer_llvm_type`'s matching cases (used to size an
        # alloca), which already handle these; without this, binding a
        # struct-returning `(let p (if cond (make_a) (make_b)))` with no
        # `&T` annotation lost the Nyet name the same way every other
        # case fixed in this session did, even though the *LLVM* type
        # ("ptr") was already inferred correctly.
        if isinstance(node, N.If) and node.then_branch:
            return self._infer_nyet_type_name(node.then_branch)
        if isinstance(node, N.Match) and node.arms:
            return self._infer_nyet_type_name(node.arms[0].body)
        if isinstance(node, N.Do) and node.exprs:
            prior_let = self._find_do_tail_let(node)
            if prior_let is not None:
                if prior_let.type is not None:
                    return self._nyet_type_name(prior_let.type)
                if prior_let.value is not None:
                    return self._infer_nyet_type_name(prior_let.value)
            return self._infer_nyet_type_name(node.exprs[-1])
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
            return self._emit_array_assign(target.head.name, target.args[0], node.value)
        # Indexed assignment into an `Array[T]` value produced by a
        # further expression: `(= ((. v data) i) x)` or
        # `(= ((grid i) j) x)`. See `_emit_array_assign_nested`.
        if (
            isinstance(target, N.Call)
            and isinstance(target.head, (N.FieldAccess, N.Call))
            and len(target.args) == 1
            and self._array_elem_ty_of_expr(target.head) is not None
        ):
            return self._emit_array_assign_nested(target.head, target.args[0], node.value)
        # Map insert/update: `(= (m key) v)`.
        if (
            isinstance(target, N.Call)
            and isinstance(target.head, N.Ident)
            and target.head.name in self._env_map_val_ty
            and len(target.args) == 1
        ):
            return self._emit_map_set(target.head.name, target.args[0], node.value)
        # Field assignment: `(= (. p x) v)`.
        if isinstance(target, N.FieldAccess):
            ptr_and_ty = self._emit_field_ptr(target)
            if ptr_and_ty is not None:
                fptr, ftype = ptr_and_ty
                val = self._emit_expr(node.value)
                if val is not None:
                    self._emit_line(f"store {ftype} {val}, ptr {fptr}")
            return None
        if isinstance(target, N.Ident) and target.name in self._env:
            ptr, ty = self._env[target.name]
            if target.name in self._env_array_elem:
                # `(= arr (array_new n))` -- reassigning a `var Array[T]`
                # to a freshly-allocated array (e.g. a grow-by-reallocate
                # pattern). See `_emit_expr_as_array`.
                val = self._emit_expr_as_array(node.value, self._env_array_elem[target.name])
            else:
                val = self._emit_expr(node.value)
            if val is not None:
                self._emit_line(f"store {ty} {val}, ptr {ptr}")
            return None
        if isinstance(target, N.Ident):
            # Same "undefined identifier at codegen time" case _emit_ident
            # raises for a read -- most commonly a `(fn! ...)` closure
            # writing a variable from its enclosing scope, which
            # `_lift_closures` hoists to a function with no access to it.
            # Falling through to the generic `return None` below used to
            # silently skip the assignment (and never even evaluate
            # `node.value`, so a read-then-write like `(= counter (+
            # counter 1))` didn't hit _emit_ident's own check either).
            raise NotImplementedError(
                f"undefined identifier '{target.name}' at codegen time in an "
                f"assignment -- most likely a `(fn! ...)` closure writing a "
                f"variable from its enclosing scope: variable capture in "
                f"closures is not implemented yet (see CONTINUATION_PLAN.md)"
            )
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

    def _emit_bounds_check(self, arr_ptr: str, idx64: str) -> None:
        """Panic (print + exit(1)) if `idx64` is out of `[0, len)` for the
        array at `arr_ptr`. Length is the i64 stored at offset 0."""
        self._declare_printf()
        self._declare_extern("declare void @exit(i32)")

        len64 = self._fresh_tmp()
        self._emit_line(f"{len64} = load i64, ptr {arr_ptr}")
        lo_ok = self._fresh_tmp()
        self._emit_line(f"{lo_ok} = icmp sge i64 {idx64}, 0")
        hi_ok = self._fresh_tmp()
        self._emit_line(f"{hi_ok} = icmp slt i64 {idx64}, {len64}")
        ok = self._fresh_tmp()
        self._emit_line(f"{ok} = and i1 {lo_ok}, {hi_ok}")

        ok_label = self._fresh_label("bounds_ok")
        panic_label = self._fresh_label("bounds_panic")
        self._emit_line(f"br i1 {ok}, label %{ok_label}, label %{panic_label}")

        self._emit_label(panic_label)
        msg = self._get_format_string(
            "index out of bounds: the len is %lld but the index is %lld\n",
            "bounds_panic_msg",
        )
        panic_tmp = self._fresh_tmp()
        self._emit_line(
            f"{panic_tmp} = call i32 (ptr, ...) @printf(ptr {msg}, i64 {len64}, i64 {idx64})"
        )
        self._emit_line("call void @exit(i32 1)")
        self._emit_line("unreachable")

        self._emit_label(ok_label)

    def _emit_array_len(self, arg: N.Expr) -> str:
        """Read the i64 length stored at offset 0 of an array's heap block,
        then truncate to i32 so it can be used in `i32` arithmetic and
        comparisons without explicit casts.

        `string` is `ptr`-shaped exactly like `Array[T]`, but has no
        such length header -- it's a plain null-terminated C string
        (see `_emit_string_concat`'s doc comment) -- so `(len s)` on a
        string previously read whatever bytes happened to sit at the
        start of its character data as if they were an i64 length,
        returning garbage (confirmed: `(len "Alexandria")` returned
        2019912769, not 10). `require_nonempty` in minibase_core.no
        (`(!= (len s) 0)`) has been silently broken this whole time as
        a result -- any blank-string validation built on `len` never
        actually validated anything. Fixed by routing a string operand
        through `strlen` instead, via the same `_is_string_operand`
        classifier `_emit_cmp` already uses to give `string` its own
        `==`/`!=`/`<`/`>` behavior.
        """
        target = self._unwrap_borrow(arg)
        if self._is_string_operand(target):
            val = self._emit_expr(target)
            if val is None:
                return "0"
            self._declare_extern("declare i64 @strlen(ptr)")
            slen64 = self._fresh_tmp()
            self._emit_line(f"{slen64} = call i64 @strlen(ptr {val})")
            slen32 = self._fresh_tmp()
            self._emit_line(f"{slen32} = trunc i64 {slen64} to i32")
            return slen32
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
        elem_ty = self._infer_llvm_type(node.elements[0]) if node.elements else "i32"
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
                    self._emit_line(f"{slot} = getelementptr {elem_ty}, ptr {base}, i64 {i}")
                self._emit_line(f"store {elem_ty} {val}, ptr {slot}")
        return arr_ptr

    def _emit_array_new(self, n_arg: N.Expr, elem_ty: str) -> str | None:
        """`(array_new n)` -- the one core primitive that lets Nyet
        source allocate a runtime-sized Array[T], for stdlib code
        (std/) to build on instead of every collection operation living
        in the Python compiler. Same malloc + `[i64 length]` header as
        `_emit_array_lit`, minus the per-element stores: contents are
        UNINITIALIZED, and the caller must fill every slot via indexed
        assignment before reading it back (exactly how
        `_emit_hof_map`/`_emit_hof_filter` already build their own
        result arrays internally -- this just exposes the same pattern
        as a callable). `array_new` isn't dispatched through the
        general `_emit_call` builtin table like `map`/`filter` since,
        unlike them, it needs an element type that isn't recoverable
        from its own argument (a bare integer count) -- every caller
        that already knows the expected `Array[T]` shape from context
        should go through `_emit_expr_as_array` instead of calling this
        directly.
        """
        n_val = self._emit_expr(n_arg)
        if n_val is None:
            return None
        n_ty = self._infer_llvm_type(n_arg)
        n64 = self._idx_to_i64(n_val, n_ty)

        out_size = self._fresh_tmp()
        self._emit_line(f"{out_size} = mul i64 {n64}, {self._sizeof(elem_ty)}")
        total = self._fresh_tmp()
        self._emit_line(f"{total} = add i64 {out_size}, 8")
        self._declare_extern("declare ptr @malloc(i64)")
        out = self._fresh_tmp()
        self._emit_line(f"{out} = call ptr @malloc(i64 {total})")
        self._emit_line(f"store i64 {n64}, ptr {out}")
        return out

    def _emit_expr_as_array(self, node: N.Node | None, elem_ty: str) -> str | None:
        """Emit `node` knowing it's expected to produce an `Array[T]`
        with LLVM element type `elem_ty`.

        Before this, `(array_new n)` only ever worked as the literal,
        direct RHS of a `let`/`var` with an explicit `Array[T]`
        annotation (`_emit_let`'s own special case) -- anywhere else
        `array_new` was written (a function's implicit tail-return
        value, an explicit `(return (array_new n))`, a `do` block's
        tail position, reassigning an existing array `var`), it fell
        through the ordinary `_emit_call` dispatch to `_emit_user_call`,
        which has no idea `array_new` means anything special and
        compiled it as a call to an undefined external function
        (`call i32 @array_new(...)`, the wrong return type entirely --
        a hard clang build failure, not a silent miscompile). This is
        the single choke point every one of those call sites now routes
        through instead of a plain `_emit_expr`, recursing into a `do`
        block's tail position (mirroring how `_infer_llvm_type` already
        recurses into `do`/`if` tails for ordinary type inference) so
        `(do ... (array_new n))` works the same as a bare `(array_new n)`.
        """
        if (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Ident)
            and node.head.name == "array_new"
            and len(node.args) == 1
        ):
            return self._emit_array_new(node.args[0], elem_ty)
        if isinstance(node, N.Do) and node.exprs:
            for stmt in node.exprs[:-1]:
                self._emit_expr(stmt)
            return self._emit_expr_as_array(node.exprs[-1], elem_ty)
        return self._emit_expr(node)

    def _emit_array_index_val(self, arr: str, elem_ty: str, idx_arg: N.Expr) -> str:
        """Core `(arr i)` lowering given an already-computed array
        pointer and element type -- shared by `_emit_array_index`
        (named local binding) and `_emit_array_index_on_field`
        (an `Array[T]`-typed struct field indexed directly, e.g.
        `((. v data) i)`, with no intermediate local binding)."""
        idx_val = self._emit_expr(idx_arg)
        idx_ty = self._infer_llvm_type(idx_arg)
        idx64 = self._idx_to_i64(idx_val or "0", idx_ty)
        self._emit_bounds_check(arr, idx64)

        base = self._array_data_base(arr)
        elem_ptr = self._fresh_tmp()
        self._emit_line(f"{elem_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {idx64}")
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {elem_ty}, ptr {elem_ptr}")
        return result

    def _emit_array_index(self, name: str, idx_arg: N.Expr) -> str:
        ptr_slot, _ = self._env[name]
        elem_ty = self._env_array_elem[name]
        arr = self._fresh_tmp()
        self._emit_line(f"{arr} = load ptr, ptr {ptr_slot}")
        return self._emit_array_index_val(arr, elem_ty, idx_arg)

    def _array_elem_ty_of_expr(self, node: N.Node) -> str | None:
        """Return the LLVM element type if evaluating `node` yields an
        `Array[T]` pointer, for the two shapes not already covered by a
        plain `Ident` bound in `_env_array_elem`:
          - a `FieldAccess` into an `Array[T]`-typed struct field, e.g.
            `(. v data)` -- see `_struct_field_array_elem`.
          - a `Call` indexing an `Array[Array[U]]`-typed binding, e.g.
            `(grid i)` -- see `_env_array_elem_of_array`.
        Used by `_emit_call`/`_emit_assign` to dispatch a further level
        of indexing directly (`((. v data) i)`, `((grid i) j)`) without
        requiring an intermediate `let` binding for the inner array."""
        if isinstance(node, N.FieldAccess):
            struct_name = self._struct_name_of(node.target)
            if struct_name is None:
                return None
            return self._struct_field_array_elem.get(struct_name, {}).get(node.field_name)
        if (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Ident)
            and node.head.name in self._env_array_elem_of_array
            and len(node.args) == 1
        ):
            return self._env_array_elem_of_array[node.head.name]
        return None

    def _emit_array_index_nested(self, head: N.Node, idx_arg: N.Expr) -> str | None:
        """`((. v data) i)` or `((grid i) j)` -- indexing an `Array[T]`
        value produced by a further expression (a struct field, or
        another array-of-arrays index), without first binding it to a
        local. Before this, `_emit_call` only ever recognized array
        indexing when its head was a plain `Ident` bound in
        `_env_array_elem`; any other head shape fell through every
        dispatch case and silently evaluated to `None` -- dropped
        output for a read, and (via `_emit_assign`'s matching gap) a
        silently no-op'd write. The FieldAccess case is precisely the
        shape a growable-Vector-style struct (`(struct Vector len:i32
        cap:i32 data:Array[T])`) needs for its own methods to index
        `data` directly; the nested-Call case is the natural way to
        index a 2D `Array[Array[U]]` grid without an intermediate
        `let`."""
        elem_ty = self._array_elem_ty_of_expr(head)
        if elem_ty is None:
            return None
        arr = self._emit_expr(head)
        if arr is None:
            return None
        return self._emit_array_index_val(arr, elem_ty, idx_arg)

    def _emit_string_index(self, name: str, idx_arg: N.Expr) -> str:
        """Lower `(str i)` to a byte load: index into the null-terminated
        UTF-8 buffer and zero-extend the byte to `i32` (a `char`).

        This is byte indexing, not codepoint indexing — for ASCII/Latin-1
        text (digits, identifiers) the two coincide; a multi-byte UTF-8
        sequence would be read one byte at a time. Bounds are the caller's
        responsibility, exactly as with array indexing.
        """
        ptr_slot, _ = self._env[name]
        strp = self._fresh_tmp()
        self._emit_line(f"{strp} = load ptr, ptr {ptr_slot}")

        idx_val = self._emit_expr(idx_arg)
        idx_ty = self._infer_llvm_type(idx_arg)
        idx64 = self._idx_to_i64(idx_val or "0", idx_ty)

        elem_ptr = self._fresh_tmp()
        self._emit_line(f"{elem_ptr} = getelementptr i8, ptr {strp}, i64 {idx64}")
        byte = self._fresh_tmp()
        self._emit_line(f"{byte} = load i8, ptr {elem_ptr}")
        ch = self._fresh_tmp()
        self._emit_line(f"{ch} = zext i8 {byte} to i32")
        return ch

    def _get_or_register_tuple_type(
        self, elem_tys: list[str], elem_nyet: list[str | None] | None = None
    ) -> str:
        """Return the synthesized struct type name for a tuple shape,
        registering (and emitting a `%name = type {...}` line for) it on
        first use. Fields are named "0", "1", ... so the existing
        struct-field GEP machinery applies unchanged.

        `elem_nyet`, when given, records each element's Nyet type name
        in `_struct_field_nyet` (keyed like any other struct) so `(t i)`
        on a tuple containing a struct/sum element keeps working for a
        subsequent `(. (t i) field)` — see `_infer_nyet_type_name`'s
        tuple-indexing case. Cached by LLVM shape only (`elem_tys`), so
        two tuples that happen to share an LLVM shape (e.g. both a
        single `ptr` field) but hold *different* Nyet element types
        will share one synthesized type; whichever call registers it
        first wins for this metadata. This can't cause a wrong field
        *load* (the LLVM layout is genuinely identical either way,
        which is why sharing the type is sound at all) — worst case, a
        later caller's element is field-accessed as if it were the
        earlier caller's struct type, which would only actually go
        wrong if the two structs are unrelated types that happen to
        share a field name with a different meaning. Accepted same as
        the analogous existing limitation for `Array[T]` elements (see
        CONTINUATION_PLAN.md's generics-inference note).
        """
        key = tuple(elem_tys)
        if key in self._tuple_types:
            return self._tuple_types[key]
        name = f"tuple.{len(self._tuple_types)}"
        self._structs[name] = [(str(i), ty) for i, ty in enumerate(elem_tys)]
        llvm_fields = ", ".join(elem_tys)
        self._struct_type_lines.append(f"%{name} = type {{ {llvm_fields} }}")
        self._tuple_types[key] = name
        if elem_nyet is not None:
            self._struct_field_nyet[name] = {
                str(i): nyet for i, nyet in enumerate(elem_nyet) if nyet is not None
            }
        return name

    def _emit_tuple_lit(self, node: N.TupleLit) -> str | None:
        """Emit `#(e0 e1 ...)` -> malloc + store each element, mirroring
        struct construction (heap-allocated so tuples can be returned)."""
        elem_vals: list[str] = []
        elem_tys: list[str] = []
        elem_nyet: list[str | None] = []
        for e in node.elements:
            v = self._emit_expr(e)
            if v is None:
                return None
            elem_vals.append(v)
            elem_tys.append(self._infer_llvm_type(e))
            elem_nyet.append(self._infer_nyet_type_name(e))

        tname = self._get_or_register_tuple_type(elem_tys, elem_nyet)
        ptr = self._heap_alloc_struct(tname, self._struct_size_bytes(tname))
        for i, (v, ty) in enumerate(zip(elem_vals, elem_tys, strict=False)):
            fptr = self._fresh_tmp()
            self._emit_line(f"{fptr} = getelementptr inbounds %{tname}, ptr {ptr}, i32 0, i32 {i}")
            self._emit_line(f"store {ty} {v}, ptr {fptr}")
        return ptr

    def _emit_tuple_index(self, name: str, idx_arg: N.Expr) -> str | None:
        """Lower `(t i)` where `t` is a tuple binding -> GEP + load. The
        index must be a literal integer since tuple fields are
        heterogeneously typed (unlike Array[T])."""
        if not isinstance(idx_arg, N.IntLit):
            return None
        tname = self._env_tuple_types[name]
        fields = self._structs[tname]
        if not (0 <= idx_arg.value < len(fields)):
            return None
        _, ftype = fields[idx_arg.value]

        ptr_slot, _ = self._env[name]
        tup_ptr = self._fresh_tmp()
        self._emit_line(f"{tup_ptr} = load ptr, ptr {ptr_slot}")
        fptr = self._fresh_tmp()
        self._emit_line(
            f"{fptr} = getelementptr inbounds %{tname}, ptr {tup_ptr}, i32 0, i32 {idx_arg.value}"
        )
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load {ftype}, ptr {fptr}")
        return result

    def _emit_array_assign_val(
        self, arr: str, elem_ty: str, idx_arg: N.Expr, value: N.Expr | None
    ) -> str | None:
        """Core `(= (arr i) v)` lowering given an already-computed array
        pointer and element type -- shared by `_emit_array_assign`
        (named local binding) and `_emit_array_assign_on_field` (an
        `Array[T]`-typed struct field indexed directly)."""
        idx_val = self._emit_expr(idx_arg)
        idx_ty = self._infer_llvm_type(idx_arg)
        idx64 = self._idx_to_i64(idx_val or "0", idx_ty)
        self._emit_bounds_check(arr, idx64)

        if value is None:
            return None
        val = self._emit_expr(value)
        if val is None:
            return None

        # Coerce int→float when storing into a float-element array.
        val_ty = self._infer_llvm_type(value)
        if self._is_float(elem_ty) and not self._is_float(val_ty):
            conv = self._fresh_tmp()
            iop = "uitofp" if self._node_is_unsigned(value) else "sitofp"
            self._emit_line(f"{conv} = {iop} {val_ty} {val} to {elem_ty}")
            val = conv

        base = self._array_data_base(arr)
        elem_ptr = self._fresh_tmp()
        self._emit_line(f"{elem_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {idx64}")
        self._emit_line(f"store {elem_ty} {val}, ptr {elem_ptr}")
        return None

    def _emit_array_assign(self, name: str, idx_arg: N.Expr, value: N.Expr | None) -> str | None:
        ptr_slot, _ = self._env[name]
        elem_ty = self._env_array_elem[name]
        arr = self._fresh_tmp()
        self._emit_line(f"{arr} = load ptr, ptr {ptr_slot}")
        return self._emit_array_assign_val(arr, elem_ty, idx_arg, value)

    def _emit_array_assign_nested(
        self, head: N.Node, idx_arg: N.Expr, value: N.Expr | None
    ) -> str | None:
        """`(= ((. v data) i) x)` or `(= ((grid i) j) x)` -- the
        write-side counterpart to `_emit_array_index_nested`; see its
        docstring."""
        elem_ty = self._array_elem_ty_of_expr(head)
        if elem_ty is None:
            return None
        arr = self._emit_expr(head)
        if arr is None:
            return None
        return self._emit_array_assign_val(arr, elem_ty, idx_arg, value)

    # ------------------------------------------------------------------
    # Higher-order functions over Array[T]: map, filter, fold, any, all, zip
    # ------------------------------------------------------------------
    #
    # `f` arguments always arrive as an N.Ident after lambda lifting (see
    # `_lift_in`, which rewrites every inline `(fn ...)` into a top-level
    # N.FnDecl and replaces the literal with a reference to it) -- so
    # resolving "the callable" is uniform whether it's a named top-level
    # function or a closure that used to be an inline lambda.

    def _resolve_callable(
        self, node: N.Expr, elem_ty: str | None = None
    ) -> tuple[str, list[str], str] | None:
        """Resolve a callable expression to (fnptr_value, param_types, ret_type).

        `elem_ty` is the element type of the array a HOF is iterating --
        needed to give an operator used as a function value (`(fold + 0
        xs)`, `(fold min (xs 0) xs)`) a concrete signature."""
        if not isinstance(node, N.Ident):
            return None
        # The first element returned is a closure value -- see
        # `_emit_closure_value`. A local fn-typed binding shadows a global.
        if node.name in self._env_fn_sig and node.name in self._env:
            ptr, _ = self._env[node.name]
            closure = self._fresh_tmp()
            self._emit_line(f"{closure} = load ptr, ptr {ptr}")
            sig_types, ret_type = self._env_fn_sig[node.name]
            return closure, list(sig_types), ret_type
        if node.name in self._closure_info:
            self._infer_untyped_lambda(node.name, elem_ty)
        if node.name in self._fn_sigs:
            param_tys, ret_ty = self._fn_sigs[node.name]
            closure_val = self._emit_ident(node)
            if closure_val is None:
                return None
            return closure_val, list(param_tys), ret_ty
        if elem_ty is not None and node.name in self._OP_CALLABLES and node.name not in self._env:
            name = self._get_or_emit_op_fn(node.name, elem_ty)
            return self._static_closure_record(name), [elem_ty, elem_ty], elem_ty
        return None

    def _infer_untyped_lambda(self, name: str, elem_ty: str | None) -> None:
        """Fill in a lifted lambda's missing types from the HOF it's passed
        to: untyped params take the array's element type, and a missing
        return type is inferred from the body -- `(filter (fn (x) (> x 2))
        nums)`. Without this, untyped params defaulted to i32 and a missing
        return type to void."""
        decl = self._lifted_decls.get(name)
        if decl is None or elem_ty is None:
            return
        untyped = [p for p in decl.params if p.type is None]
        if not untyped and decl.return_type is not None:
            return
        for p in untyped:
            p.type = self._type_node_for_llvm(elem_ty, decl.span)
        if decl.return_type is None and decl.body is not None:
            saved_env = dict(self._env)
            for p in decl.params:
                self._env[p.name] = ("%__infer", self._llvm_type(p.type))
            try:
                ret_ty = self._infer_llvm_type(decl.body)
            finally:
                self._env = saved_env
            decl.return_type = self._type_node_for_llvm(ret_ty, decl.span)
        self._register_fn_sig(decl)

    @staticmethod
    def _type_node_for_llvm(ty: str, span: N.Span) -> N.TypeNode:
        names = {
            "i1": "bool",
            "i8": "u8",
            "i16": "i16",
            "i32": "i32",
            "i64": "i64",
            "float": "f32",
            "double": "f64",
            "ptr": "string",
            "void": "unit",
        }
        return N.PrimType(span, names.get(ty, "i32"))

    # Operators (and min/max) usable as a two-argument function value.
    _OP_CALLABLES = {
        "+": "add",
        "-": "sub",
        "*": "mul",
        "/": "div",
        "%": "rem",
        "min": "min",
        "max": "max",
    }

    def _get_or_emit_op_fn(self, op: str, ty: str) -> str:
        """Synthesize `define internal ty @__nyet_op_<op>_<ty>(ty, ty)` for
        an operator used as a function value, or `min`/`max`. Signed
        integer semantics; strings aren't supported."""
        if ty == "ptr" or ty == "void":
            raise NotImplementedError(
                f"codegen: `{op}` used as a function value over `{ty}` operands is not supported"
            )
        name = f"__nyet_op_{self._OP_CALLABLES[op]}_{ty}"
        if name in self._synth_fns:
            return name
        is_float = self._is_float(ty)
        if op in ("min", "max"):
            cmp = "fcmp" if is_float else "icmp"
            if is_float:
                pred = "olt" if op == "min" else "ogt"
            else:
                pred = "slt" if op == "min" else "sgt"
            body = [f"  %c = {cmp} {pred} {ty} %a, %b", f"  %r = select i1 %c, {ty} %a, {ty} %b"]
        else:
            if is_float:
                ops = {"+": "fadd", "-": "fsub", "*": "fmul", "/": "fdiv", "%": "frem"}
            else:
                ops = {"+": "add", "-": "sub", "*": "mul", "/": "sdiv", "%": "srem"}
            body = [f"  %r = {ops[op]} {ty} %a, %b"]
        self._synth_fns[name] = "\n".join(
            [
                f"define internal {ty} @{name}(ptr %__env, {ty} %a, {ty} %b) {{",
                "entry:",
                *body,
                f"  ret {ty} %r",
                "}",
                "",
            ]
        )
        return name

    @staticmethod
    def _unresolved_callable_msg(f_arg: N.Expr) -> str:
        what = f"'{f_arg.name}'" if isinstance(f_arg, N.Ident) else type(f_arg).__name__
        return (
            f"codegen: can't use {what} as the function argument of a higher-order "
            f"builtin -- expected a named function, a closure, a fn-typed binding, "
            f"or an operator"
        )

    def _convert_numeric(self, val: str, from_ty: str, to_ty: str, node: N.Node) -> str:
        """Convert a numeric SSA value between LLVM int/float types."""
        if from_ty == to_ty:
            return val
        if self._is_float(to_ty) and not self._is_float(from_ty):
            conv = self._fresh_tmp()
            iop = "uitofp" if self._node_is_unsigned(node) else "sitofp"
            self._emit_line(f"{conv} = {iop} {from_ty} {val} to {to_ty}")
            return conv
        if self._is_float(to_ty) and self._is_float(from_ty):
            conv = self._fresh_tmp()
            fop = "fpext" if to_ty == "double" else "fptrunc"
            self._emit_line(f"{conv} = {fop} {from_ty} {val} to {to_ty}")
            return conv
        if not self._is_float(from_ty) and from_ty != "ptr" and to_ty != "ptr":
            return self._coerce_int_to(val, from_ty, to_ty, self._node_is_unsigned(node))
        raise NotImplementedError(f"codegen: can't convert a `{from_ty}` value to `{to_ty}`")

    def _min_max_type(self, args: list[N.Expr]) -> str:
        lty = self._infer_llvm_type(args[0])
        rty = self._infer_llvm_type(args[1])
        if lty == rty:
            return lty
        if self._is_float(lty) or self._is_float(rty):
            return "double"
        return self._common_cmp_type(args[0], args[1], lty, rty)

    def _emit_min_max(self, op: str, args: list[N.Expr]) -> str:
        """`(min a b)` / `(max a b)` on two numbers."""
        lhs = self._emit_expr(args[0])
        rhs = self._emit_expr(args[1])
        if lhs is None or rhs is None:
            raise NotImplementedError(f"codegen: an operand of `{op}` produced no value")
        ty = self._min_max_type(args)
        lhs = self._convert_numeric(lhs, self._infer_llvm_type(args[0]), ty, args[0])
        rhs = self._convert_numeric(rhs, self._infer_llvm_type(args[1]), ty, args[1])
        fn = self._get_or_emit_op_fn(op, ty)
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call {ty} @{fn}(ptr null, {ty} {lhs}, {ty} {rhs})")
        return tmp

    def _is_parse_call(self, node: N.Node | None) -> bool:
        return (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Ident)
            and node.head.name == "parse"
            and len(node.args) == 1
            and "parse" not in self._fn_sigs
        )

    def _parse_result_type(self) -> str:
        """`Result[i32 string]`, the type `parse` returns, monomorphized from
        the program's own `Result[T E]` (main.no declares it)."""
        mangled = (
            self._monomorphize_sum_type("Result", ("i32", "string"))
            if "Result" in self._sum_templates
            else None
        )
        if mangled is None:
            raise NotImplementedError(
                "codegen: `parse` returns `Result[i32 string]`, which needs a generic "
                "`Result[T E]` sum type with `Ok` and `Err` variants"
            )
        return mangled

    def _emit_sum_raw(self, sum_name: str, tag: int, payload_vals: list[str]) -> str:
        """Construct a sum value from already-emitted payload values, laid out
        exactly like `_emit_variant_construct`."""
        _, payload_types = self._sum_types[sum_name][tag]
        ptr = self._heap_alloc_struct(sum_name, self._sum_type_size_bytes(sum_name))
        tag_ptr = self._fresh_tmp()
        self._emit_line(f"{tag_ptr} = getelementptr inbounds %{sum_name}, ptr {ptr}, i32 0, i32 0")
        self._emit_line(f"store i32 {tag}, ptr {tag_ptr}")
        if payload_types:
            payload_ptr = self._fresh_tmp()
            self._emit_line(
                f"{payload_ptr} = getelementptr inbounds %{sum_name}, ptr {ptr}, i32 0, i32 1"
            )
            offsets = self._field_offsets(payload_types)
            for ftype, off, val in zip(payload_types, offsets, payload_vals, strict=False):
                field_ptr = payload_ptr
                if off != 0:
                    field_ptr = self._fresh_tmp()
                    self._emit_line(f"{field_ptr} = getelementptr i8, ptr {payload_ptr}, i32 {off}")
                self._emit_line(f"store {ftype} {val}, ptr {field_ptr}")
        return ptr

    def _emit_parse(self, arg: N.Expr) -> str:
        """`(parse s)` -> `(Ok n)` if the whole string (optionally followed by a
        newline, as `(in)` returns it) is a decimal i32, else `(Err message)`.
        It used to compile to a call to an undefined `@parse`."""
        sum_name = self._parse_result_type()
        tags = {vname: i for i, (vname, _) in enumerate(self._sum_types[sum_name])}
        if "Ok" not in tags or "Err" not in tags:
            raise NotImplementedError("codegen: `parse` needs `Result`'s `Ok` and `Err` variants")
        text = self._emit_expr(arg)
        if text is None:
            raise NotImplementedError(
                f"codegen: the `parse` argument ({arg.span}) produced no value"
            )
        self._declare_extern("declare i64 @strtol(ptr, ptr, i32)")
        end_slot = self._emit_alloca("ptr")
        result_slot = self._emit_alloca("ptr")
        value64 = self._fresh_tmp()
        self._emit_line(f"{value64} = call i64 @strtol(ptr {text}, ptr {end_slot}, i32 10)")
        end = self._fresh_tmp()
        self._emit_line(f"{end} = load ptr, ptr {end_slot}")
        consumed = self._fresh_tmp()
        self._emit_line(f"{consumed} = icmp ne ptr {end}, {text}")
        last = self._fresh_tmp()
        self._emit_line(f"{last} = load i8, ptr {end}")
        at_nul = self._fresh_tmp()
        self._emit_line(f"{at_nul} = icmp eq i8 {last}, 0")
        at_newline = self._fresh_tmp()
        self._emit_line(f"{at_newline} = icmp eq i8 {last}, 10")
        at_end = self._fresh_tmp()
        self._emit_line(f"{at_end} = or i1 {at_nul}, {at_newline}")
        ok = self._fresh_tmp()
        self._emit_line(f"{ok} = and i1 {consumed}, {at_end}")
        ok_label = self._fresh_label("parse_ok")
        err_label = self._fresh_label("parse_err")
        done_label = self._fresh_label("parse_done")
        self._emit_line(f"br i1 {ok}, label %{ok_label}, label %{err_label}")

        self._emit_label(ok_label)
        value32 = self._fresh_tmp()
        self._emit_line(f"{value32} = trunc i64 {value64} to i32")
        ok_val = self._emit_sum_raw(sum_name, tags["Ok"], [value32])
        self._emit_line(f"store ptr {ok_val}, ptr {result_slot}")
        self._emit_line(f"br label %{done_label}")

        self._emit_label(err_label)
        message, _ = self._get_string("invalid integer")
        err_val = self._emit_sum_raw(sum_name, tags["Err"], [message])
        self._emit_line(f"store ptr {err_val}, ptr {result_slot}")
        self._emit_line(f"br label %{done_label}")

        self._emit_label(done_label)
        result = self._fresh_tmp()
        self._emit_line(f"{result} = load ptr, ptr {result_slot}")
        return result

    def _emit_sqrt(self, arg: N.Expr) -> str:
        """`(sqrt x)` -> `f64`, via the `llvm.sqrt` intrinsic."""
        val = self._emit_expr(arg)
        if val is None:
            raise NotImplementedError(
                f"codegen: the `sqrt` argument ({arg.span}) produced no value"
            )
        val = self._convert_numeric(val, self._infer_llvm_type(arg), "double", arg)
        self._declare_extern("declare double @llvm.sqrt.f64(double)")
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call double @llvm.sqrt.f64(double {val})")
        return tmp

    def _array_expr_elem_ty(self, node: N.Node) -> str | None:
        """LLVM element type of an array-valued expression, if known
        without emitting it: a named array binding, an inline literal, a
        nested `map`/`filter` call, or the shapes `_array_elem_ty_of_expr`
        covers (struct field, 2D grid row)."""
        if isinstance(node, N.Ident):
            return self._env_array_elem.get(node.name)
        if isinstance(node, N.ArrayLit):
            return self._infer_llvm_type(node.elements[0]) if node.elements else None
        if (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Ident)
            and node.head.name in self._fn_ret_array_elem
        ):
            return self._fn_ret_array_elem[node.head.name]
        if (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Ident)
            and node.head.name not in self._fn_sigs
            and len(node.args) == 2
        ):
            if node.head.name == "filter":
                return self._array_expr_elem_ty(node.args[1])
            if node.head.name == "flat_map" and isinstance(node.args[0], N.Ident):
                return self._fn_ret_array_elem.get(node.args[0].name)
            if node.head.name == "map" and isinstance(node.args[0], N.Ident):
                f = node.args[0].name
                if f in self._closure_info:
                    self._infer_untyped_lambda(f, self._array_expr_elem_ty(node.args[1]))
                if f in self._fn_sigs:
                    return self._fn_sigs[f][1]
                if f in self._env_fn_sig:
                    return self._env_fn_sig[f][1]
        return self._array_elem_ty_of_expr(node)

    def _hof_array_arg(self, arg: N.Expr) -> str | None:
        """Name of an `Array[T]` binding holding `arg`'s value, for the HOF
        builtins (which iterate a named binding). A plain array binding is
        used as-is; any other array-valued expression -- an inline literal
        `[1 2 3]`, a nested HOF call produced by a `|>` pipeline -- is
        evaluated once into a hidden local. Before this, HOF dispatch only
        matched a bare identifier, so `(fold + 0 (map f xs))` fell through
        to a call to an undefined `@fold`. Returns None if `arg` isn't
        known to be an array."""
        if isinstance(arg, N.Ident):
            return arg.name if arg.name in self._env_array_elem else None
        elem_ty = self._array_expr_elem_ty(arg)
        if elem_ty is None:
            return None
        val = self._emit_expr(arg)
        if val is None:
            return None
        self._hof_tmp_count += 1
        name = f"__hof_arr{self._hof_tmp_count}"
        slot = self._emit_alloca("ptr")
        self._emit_line(f"store ptr {val}, ptr {slot}")
        self._env[name] = (slot, "ptr")
        self._env_array_elem[name] = elem_ty
        return name

    def _emit_indirect_call_raw(
        self, fnptr: str, arg_tys: list[str], arg_vals: list[str], ret_ty: str
    ) -> str | None:
        """Call closure value `fnptr` -- a closure record, see
        `_emit_closure_value` -- with already-materialized arguments: load the
        code pointer from the record and pass the record as the hidden first
        argument."""
        code = self._fresh_tmp()
        self._emit_line(f"{code} = load ptr, ptr {fnptr}")
        args = [f"ptr {fnptr}"] + [f"{t} {v}" for t, v in zip(arg_tys, arg_vals, strict=False)]
        args_str = ", ".join(args)
        if ret_ty == "void":
            self._emit_line(f"call void {code}({args_str})")
            return None
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call {ret_ty} {code}({args_str})")
        return tmp

    def _array_ptr_and_len(self, arr_name: str) -> tuple[str, str]:
        """Load an array binding's heap ptr and its i64 length."""
        ptr_slot, _ = self._env[arr_name]
        arr = self._fresh_tmp()
        self._emit_line(f"{arr} = load ptr, ptr {ptr_slot}")
        len64 = self._fresh_tmp()
        self._emit_line(f"{len64} = load i64, ptr {arr}")
        return arr, len64

    def _emit_counted_loop(self, len64: str):
        """Emit a `for i in 0..len64` skeleton. Returns (i_slot, body_label,
        end_label); caller emits the body then must `br` back to the
        condition label (returned as the third element is the exit label —
        see call sites) and close with `_emit_label(end_label)`."""
        i_slot = self._emit_alloca("i64")
        self._emit_line(f"store i64 0, ptr {i_slot}")
        cond_label = self._fresh_label("hof_cond")
        body_label = self._fresh_label("hof_body")
        end_label = self._fresh_label("hof_end")
        self._emit_line(f"br label %{cond_label}")
        self._emit_label(cond_label)
        i_val = self._fresh_tmp()
        self._emit_line(f"{i_val} = load i64, ptr {i_slot}")
        cmp = self._fresh_tmp()
        self._emit_line(f"{cmp} = icmp slt i64 {i_val}, {len64}")
        self._emit_line(f"br i1 {cmp}, label %{body_label}, label %{end_label}")
        self._emit_label(body_label)
        return i_slot, i_val, cond_label, end_label

    def _emit_hof_map(self, f_arg: N.Expr, arr_name: str) -> str | None:
        callable_ = self._resolve_callable(f_arg, self._env_array_elem[arr_name])
        if callable_ is None:
            raise NotImplementedError(self._unresolved_callable_msg(f_arg))
        fnptr, param_tys, ret_ty = callable_
        elem_ty = self._env_array_elem[arr_name]

        arr, len64 = self._array_ptr_and_len(arr_name)
        base = self._array_data_base(arr)

        if ret_ty == "void":
            # `(map (fn (item:&T) -> unit ...) items)` for its side effects, as
            # main.no's `print_all` does: call f on each element, with no result
            # array. This used to allocate and store into an array of `void`.
            i_slot, i_val, cond_label, end_label = self._emit_counted_loop(len64)
            src_ptr = self._fresh_tmp()
            self._emit_line(f"{src_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {i_val}")
            elem = self._fresh_tmp()
            self._emit_line(f"{elem} = load {elem_ty}, ptr {src_ptr}")
            arg_ty = param_tys[0] if param_tys else elem_ty
            self._emit_indirect_call_raw(fnptr, [arg_ty], [elem], "void")
            i_next = self._fresh_tmp()
            self._emit_line(f"{i_next} = add i64 {i_val}, 1")
            self._emit_line(f"store i64 {i_next}, ptr {i_slot}")
            self._emit_line(f"br label %{cond_label}")
            self._emit_label(end_label)
            return None

        out_size = self._fresh_tmp()
        self._emit_line(f"{out_size} = mul i64 {len64}, {self._sizeof(ret_ty)}")
        total = self._fresh_tmp()
        self._emit_line(f"{total} = add i64 {out_size}, 8")
        self._declare_extern("declare ptr @malloc(i64)")
        out = self._fresh_tmp()
        self._emit_line(f"{out} = call ptr @malloc(i64 {total})")
        self._emit_line(f"store i64 {len64}, ptr {out}")
        out_base = self._array_data_base(out)

        i_slot, i_val, cond_label, end_label = self._emit_counted_loop(len64)
        src_ptr = self._fresh_tmp()
        self._emit_line(f"{src_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {i_val}")
        elem = self._fresh_tmp()
        self._emit_line(f"{elem} = load {elem_ty}, ptr {src_ptr}")
        arg_ty = param_tys[0] if param_tys else elem_ty
        result = self._emit_indirect_call_raw(fnptr, [arg_ty], [elem], ret_ty)
        dst_ptr = self._fresh_tmp()
        self._emit_line(f"{dst_ptr} = getelementptr {ret_ty}, ptr {out_base}, i64 {i_val}")
        self._emit_line(f"store {ret_ty} {result if result is not None else '0'}, ptr {dst_ptr}")
        i_next = self._fresh_tmp()
        self._emit_line(f"{i_next} = add i64 {i_val}, 1")
        self._emit_line(f"store i64 {i_next}, ptr {i_slot}")
        self._emit_line(f"br label %{cond_label}")
        self._emit_label(end_label)
        return out

    def _emit_hof_flat_map(self, f_arg: N.Expr, arr_name: str) -> str | None:
        """`(flat_map f arr)`: call `f` -- which returns an `Array[U]` -- on
        every element and concatenate the results into one `Array[U]`."""
        callable_ = self._resolve_callable(f_arg, self._env_array_elem[arr_name])
        if callable_ is None:
            raise NotImplementedError(self._unresolved_callable_msg(f_arg))
        fnptr, param_tys, _ = callable_
        out_elem = self._fn_ret_array_elem.get(f_arg.name) if isinstance(f_arg, N.Ident) else None
        if out_elem is None:
            raise NotImplementedError(
                "codegen: `flat_map`'s function must be declared to return an `Array[T]`"
            )
        elem_ty = self._env_array_elem[arr_name]
        elem_size = self._sizeof(out_elem)
        self._declare_extern("declare ptr @malloc(i64)")
        self._declare_extern("declare ptr @memcpy(ptr, ptr, i64)")

        arr, len64 = self._array_ptr_and_len(arr_name)
        base = self._array_data_base(arr)

        # Pass 1: call f on every element, keeping each result array and a
        # running total of their lengths.
        parts_bytes = self._fresh_tmp()
        self._emit_line(f"{parts_bytes} = mul i64 {len64}, 8")
        parts = self._fresh_tmp()
        self._emit_line(f"{parts} = call ptr @malloc(i64 {parts_bytes})")
        total_slot = self._emit_alloca("i64")
        self._emit_line(f"store i64 0, ptr {total_slot}")
        i_slot, i_val, cond_label, end_label = self._emit_counted_loop(len64)
        src_ptr = self._fresh_tmp()
        self._emit_line(f"{src_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {i_val}")
        elem = self._fresh_tmp()
        self._emit_line(f"{elem} = load {elem_ty}, ptr {src_ptr}")
        arg_ty = param_tys[0] if param_tys else elem_ty
        part = self._emit_indirect_call_raw(fnptr, [arg_ty], [elem], "ptr")
        part_slot = self._fresh_tmp()
        self._emit_line(f"{part_slot} = getelementptr ptr, ptr {parts}, i64 {i_val}")
        self._emit_line(f"store ptr {part}, ptr {part_slot}")
        part_len = self._fresh_tmp()
        self._emit_line(f"{part_len} = load i64, ptr {part}")
        total = self._fresh_tmp()
        self._emit_line(f"{total} = load i64, ptr {total_slot}")
        new_total = self._fresh_tmp()
        self._emit_line(f"{new_total} = add i64 {total}, {part_len}")
        self._emit_line(f"store i64 {new_total}, ptr {total_slot}")
        i_next = self._fresh_tmp()
        self._emit_line(f"{i_next} = add i64 {i_val}, 1")
        self._emit_line(f"store i64 {i_next}, ptr {i_slot}")
        self._emit_line(f"br label %{cond_label}")
        self._emit_label(end_label)

        # Pass 2: allocate the result and copy every part into it in order.
        final_total = self._fresh_tmp()
        self._emit_line(f"{final_total} = load i64, ptr {total_slot}")
        data_bytes = self._fresh_tmp()
        self._emit_line(f"{data_bytes} = mul i64 {final_total}, {elem_size}")
        out_bytes = self._fresh_tmp()
        self._emit_line(f"{out_bytes} = add i64 {data_bytes}, 8")
        out = self._fresh_tmp()
        self._emit_line(f"{out} = call ptr @malloc(i64 {out_bytes})")
        self._emit_line(f"store i64 {final_total}, ptr {out}")
        out_base = self._array_data_base(out)
        off_slot = self._emit_alloca("i64")
        self._emit_line(f"store i64 0, ptr {off_slot}")
        j_slot, j_val, cond2_label, end2_label = self._emit_counted_loop(len64)
        part_ptr = self._fresh_tmp()
        self._emit_line(f"{part_ptr} = getelementptr ptr, ptr {parts}, i64 {j_val}")
        part2 = self._fresh_tmp()
        self._emit_line(f"{part2} = load ptr, ptr {part_ptr}")
        part2_len = self._fresh_tmp()
        self._emit_line(f"{part2_len} = load i64, ptr {part2}")
        part2_base = self._array_data_base(part2)
        off = self._fresh_tmp()
        self._emit_line(f"{off} = load i64, ptr {off_slot}")
        off_bytes = self._fresh_tmp()
        self._emit_line(f"{off_bytes} = mul i64 {off}, {elem_size}")
        dst = self._fresh_tmp()
        self._emit_line(f"{dst} = getelementptr i8, ptr {out_base}, i64 {off_bytes}")
        copy_bytes = self._fresh_tmp()
        self._emit_line(f"{copy_bytes} = mul i64 {part2_len}, {elem_size}")
        copied = self._fresh_tmp()
        self._emit_line(
            f"{copied} = call ptr @memcpy(ptr {dst}, ptr {part2_base}, i64 {copy_bytes})"
        )
        new_off = self._fresh_tmp()
        self._emit_line(f"{new_off} = add i64 {off}, {part2_len}")
        self._emit_line(f"store i64 {new_off}, ptr {off_slot}")
        j_next = self._fresh_tmp()
        self._emit_line(f"{j_next} = add i64 {j_val}, 1")
        self._emit_line(f"store i64 {j_next}, ptr {j_slot}")
        self._emit_line(f"br label %{cond2_label}")
        self._emit_label(end2_label)
        return out

    def _emit_hof_filter(self, f_arg: N.Expr, arr_name: str) -> str | None:
        callable_ = self._resolve_callable(f_arg, self._env_array_elem[arr_name])
        if callable_ is None:
            raise NotImplementedError(self._unresolved_callable_msg(f_arg))
        fnptr, param_tys, _ = callable_
        elem_ty = self._env_array_elem[arr_name]

        arr, len64 = self._array_ptr_and_len(arr_name)
        base = self._array_data_base(arr)

        # Over-allocate to the input's worst case (every element matches);
        # the true count is tracked separately and stored as the final
        # length header once known.
        out_size = self._fresh_tmp()
        self._emit_line(f"{out_size} = mul i64 {len64}, {self._sizeof(elem_ty)}")
        total = self._fresh_tmp()
        self._emit_line(f"{total} = add i64 {out_size}, 8")
        self._declare_extern("declare ptr @malloc(i64)")
        out = self._fresh_tmp()
        self._emit_line(f"{out} = call ptr @malloc(i64 {total})")
        out_base = self._array_data_base(out)

        out_i_slot = self._emit_alloca("i64")
        self._emit_line(f"store i64 0, ptr {out_i_slot}")

        i_slot, i_val, cond_label, end_label = self._emit_counted_loop(len64)
        src_ptr = self._fresh_tmp()
        self._emit_line(f"{src_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {i_val}")
        elem = self._fresh_tmp()
        self._emit_line(f"{elem} = load {elem_ty}, ptr {src_ptr}")
        arg_ty = param_tys[0] if param_tys else elem_ty
        keep = self._emit_indirect_call_raw(fnptr, [arg_ty], [elem], "i1")

        keep_label = self._fresh_label("hof_keep")
        skip_label = self._fresh_label("hof_skip")
        self._emit_line(f"br i1 {keep}, label %{keep_label}, label %{skip_label}")
        self._emit_label(keep_label)
        out_i = self._fresh_tmp()
        self._emit_line(f"{out_i} = load i64, ptr {out_i_slot}")
        dst_ptr = self._fresh_tmp()
        self._emit_line(f"{dst_ptr} = getelementptr {elem_ty}, ptr {out_base}, i64 {out_i}")
        self._emit_line(f"store {elem_ty} {elem}, ptr {dst_ptr}")
        out_i_next = self._fresh_tmp()
        self._emit_line(f"{out_i_next} = add i64 {out_i}, 1")
        self._emit_line(f"store i64 {out_i_next}, ptr {out_i_slot}")
        self._emit_line(f"br label %{skip_label}")
        self._emit_label(skip_label)

        i_next = self._fresh_tmp()
        self._emit_line(f"{i_next} = add i64 {i_val}, 1")
        self._emit_line(f"store i64 {i_next}, ptr {i_slot}")
        self._emit_line(f"br label %{cond_label}")
        self._emit_label(end_label)

        final_count = self._fresh_tmp()
        self._emit_line(f"{final_count} = load i64, ptr {out_i_slot}")
        self._emit_line(f"store i64 {final_count}, ptr {out}")
        return out

    def _emit_hof_fold(self, f_arg: N.Expr, init_arg: N.Expr, arr_name: str) -> str | None:
        callable_ = self._resolve_callable(f_arg, self._env_array_elem[arr_name])
        if callable_ is None:
            raise NotImplementedError(self._unresolved_callable_msg(f_arg))
        fnptr, param_tys, ret_ty = callable_
        elem_ty = self._env_array_elem[arr_name]

        init_val = self._emit_expr(init_arg)
        if init_val is None:
            return None
        # e.g. `(fold + 0 floats)`: the literal `0` is an i32 but the
        # accumulator is a double.
        init_val = self._convert_numeric(
            init_val, self._infer_llvm_type(init_arg), ret_ty, init_arg
        )
        acc_slot = self._emit_alloca(ret_ty)
        self._emit_line(f"store {ret_ty} {init_val}, ptr {acc_slot}")

        arr, len64 = self._array_ptr_and_len(arr_name)
        base = self._array_data_base(arr)

        i_slot, i_val, cond_label, end_label = self._emit_counted_loop(len64)
        src_ptr = self._fresh_tmp()
        self._emit_line(f"{src_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {i_val}")
        elem = self._fresh_tmp()
        self._emit_line(f"{elem} = load {elem_ty}, ptr {src_ptr}")
        acc = self._fresh_tmp()
        self._emit_line(f"{acc} = load {ret_ty}, ptr {acc_slot}")
        acc_ty = param_tys[0] if param_tys else ret_ty
        elem_arg_ty = param_tys[1] if len(param_tys) > 1 else elem_ty
        result = self._emit_indirect_call_raw(fnptr, [acc_ty, elem_arg_ty], [acc, elem], ret_ty)
        if result is not None:
            self._emit_line(f"store {ret_ty} {result}, ptr {acc_slot}")
        i_next = self._fresh_tmp()
        self._emit_line(f"{i_next} = add i64 {i_val}, 1")
        self._emit_line(f"store i64 {i_next}, ptr {i_slot}")
        self._emit_line(f"br label %{cond_label}")
        self._emit_label(end_label)

        final = self._fresh_tmp()
        self._emit_line(f"{final} = load {ret_ty}, ptr {acc_slot}")
        return final

    def _emit_hof_any_all(self, f_arg: N.Expr, arr_name: str, *, is_all: bool) -> str | None:
        callable_ = self._resolve_callable(f_arg, self._env_array_elem[arr_name])
        if callable_ is None:
            raise NotImplementedError(self._unresolved_callable_msg(f_arg))
        fnptr, param_tys, _ = callable_
        elem_ty = self._env_array_elem[arr_name]

        result_slot = self._emit_alloca("i1")
        self._emit_line(f"store i1 {'1' if is_all else '0'}, ptr {result_slot}")

        arr, len64 = self._array_ptr_and_len(arr_name)
        base = self._array_data_base(arr)

        i_slot, i_val, cond_label, end_label = self._emit_counted_loop(len64)
        src_ptr = self._fresh_tmp()
        self._emit_line(f"{src_ptr} = getelementptr {elem_ty}, ptr {base}, i64 {i_val}")
        elem = self._fresh_tmp()
        self._emit_line(f"{elem} = load {elem_ty}, ptr {src_ptr}")
        arg_ty = param_tys[0] if param_tys else elem_ty
        matched = self._emit_indirect_call_raw(fnptr, [arg_ty], [elem], "i1")

        # any: matched -> set true, stop early. all: !matched -> set false, stop early.
        trigger = matched if not is_all else self._fresh_tmp()
        if is_all:
            self._emit_line(f"{trigger} = xor i1 {matched}, true")
        hit_label = self._fresh_label("hof_hit")
        cont_label = self._fresh_label("hof_cont")
        self._emit_line(f"br i1 {trigger}, label %{hit_label}, label %{cont_label}")
        self._emit_label(hit_label)
        self._emit_line(f"store i1 {'1' if not is_all else '0'}, ptr {result_slot}")
        self._emit_line(f"br label %{end_label}")
        self._emit_label(cont_label)

        i_next = self._fresh_tmp()
        self._emit_line(f"{i_next} = add i64 {i_val}, 1")
        self._emit_line(f"store i64 {i_next}, ptr {i_slot}")
        self._emit_line(f"br label %{cond_label}")
        self._emit_label(end_label)

        final = self._fresh_tmp()
        self._emit_line(f"{final} = load i1, ptr {result_slot}")
        return final

    def _emit_hof_zip(self, arr1_name: str, arr2_name: str) -> str | None:
        elem1_ty = self._env_array_elem[arr1_name]
        elem2_ty = self._env_array_elem[arr2_name]
        elem1_nyet = self._env_array_elem_nyet.get(arr1_name)
        elem2_nyet = self._env_array_elem_nyet.get(arr2_name)
        tname = self._get_or_register_tuple_type([elem1_ty, elem2_ty], [elem1_nyet, elem2_nyet])
        tsize = self._struct_size_bytes(tname)

        arr1, len1 = self._array_ptr_and_len(arr1_name)
        arr2, len2 = self._array_ptr_and_len(arr2_name)
        base1 = self._array_data_base(arr1)
        base2 = self._array_data_base(arr2)

        shorter = self._fresh_tmp()
        cmp = self._fresh_tmp()
        self._emit_line(f"{cmp} = icmp slt i64 {len1}, {len2}")
        self._emit_line(f"{shorter} = select i1 {cmp}, i64 {len1}, i64 {len2}")

        out_size = self._fresh_tmp()
        self._emit_line(f"{out_size} = mul i64 {shorter}, {tsize}")
        total = self._fresh_tmp()
        self._emit_line(f"{total} = add i64 {out_size}, 8")
        self._declare_extern("declare ptr @malloc(i64)")
        out = self._fresh_tmp()
        self._emit_line(f"{out} = call ptr @malloc(i64 {total})")
        self._emit_line(f"store i64 {shorter}, ptr {out}")
        out_base = self._array_data_base(out)

        i_slot, i_val, cond_label, end_label = self._emit_counted_loop(shorter)
        p1 = self._fresh_tmp()
        self._emit_line(f"{p1} = getelementptr {elem1_ty}, ptr {base1}, i64 {i_val}")
        v1 = self._fresh_tmp()
        self._emit_line(f"{v1} = load {elem1_ty}, ptr {p1}")
        p2 = self._fresh_tmp()
        self._emit_line(f"{p2} = getelementptr {elem2_ty}, ptr {base2}, i64 {i_val}")
        v2 = self._fresh_tmp()
        self._emit_line(f"{v2} = load {elem2_ty}, ptr {p2}")

        tup_ptr = self._heap_alloc_struct(tname, tsize)
        f0 = self._fresh_tmp()
        self._emit_line(f"{f0} = getelementptr inbounds %{tname}, ptr {tup_ptr}, i32 0, i32 0")
        self._emit_line(f"store {elem1_ty} {v1}, ptr {f0}")
        f1 = self._fresh_tmp()
        self._emit_line(f"{f1} = getelementptr inbounds %{tname}, ptr {tup_ptr}, i32 0, i32 1")
        self._emit_line(f"store {elem2_ty} {v2}, ptr {f1}")

        dst = self._fresh_tmp()
        self._emit_line(f"{dst} = getelementptr ptr, ptr {out_base}, i64 {i_val}")
        self._emit_line(f"store ptr {tup_ptr}, ptr {dst}")

        i_next = self._fresh_tmp()
        self._emit_line(f"{i_next} = add i64 {i_val}, 1")
        self._emit_line(f"store i64 {i_next}, ptr {i_slot}")
        self._emit_line(f"br label %{cond_label}")
        self._emit_label(end_label)
        return out

    # ------------------------------------------------------------------
    # Map[string V] / Set[T] — backed by runtime/map.c
    # ------------------------------------------------------------------
    #
    # Values are stored as generic 8-byte slots in the hash table; the
    # real Nyet type is tracked statically per binding (`_env_map_val_ty`,
    # mirroring `_env_array_elem`) and used to convert to/from the slot
    # representation at each read/write, the same trick Array[T] uses to
    # stay generic without per-type monomorphized codegen.

    def _to_i64_slot(self, val: str, ty: str) -> str:
        """Widen/reinterpret a value of LLVM type `ty` to an i64 slot."""
        if ty == "i64":
            return val
        if ty in ("i1", "i8", "i16", "i32"):
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = sext {ty} {val} to i64")
            return tmp
        if ty == "ptr":
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = ptrtoint ptr {val} to i64")
            return tmp
        if ty == "double":
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = bitcast double {val} to i64")
            return tmp
        # float and anything else unhandled: widen through i32 as a
        # best-effort fallback rather than emitting invalid IR.
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = sext i32 0 to i64")
        return tmp

    def _from_i64_slot(self, val: str, ty: str) -> str:
        """Narrow/reinterpret an i64 slot back to LLVM type `ty`."""
        if ty == "i64":
            return val
        if ty in ("i1", "i8", "i16", "i32"):
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = trunc i64 {val} to {ty}")
            return tmp
        if ty == "ptr":
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = inttoptr i64 {val} to ptr")
            return tmp
        if ty == "double":
            tmp = self._fresh_tmp()
            self._emit_line(f"{tmp} = bitcast i64 {val} to double")
            return tmp
        return val

    def _emit_map_lit(self, node: N.MapLit) -> str:
        """Emit `{k1 v1 k2 v2 ...}` -> nyet_map_new + nyet_map_set per entry."""
        self._declare_extern("declare ptr @nyet_map_new(i64)")
        self._declare_extern("declare void @nyet_map_set(ptr, ptr, i64)")
        cap = max(16, len(node.entries) * 2)
        m = self._fresh_tmp()
        self._emit_line(f"{m} = call ptr @nyet_map_new(i64 {cap})")
        for k_node, v_node in node.entries:
            k_val = self._emit_expr(k_node)
            v_val = self._emit_expr(v_node)
            if k_val is None or v_val is None:
                continue
            v_ty = self._infer_llvm_type(v_node)
            v_slot = self._to_i64_slot(v_val, v_ty)
            self._emit_line(f"call void @nyet_map_set(ptr {m}, ptr {k_val}, i64 {v_slot})")
        return m

    def _emit_map_get(self, name: str, key_arg: N.Expr) -> str | None:
        self._declare_extern("declare i64 @nyet_map_get(ptr, ptr, i64)")
        val_ty = self._env_map_val_ty[name]
        ptr_slot, _ = self._env[name]
        m = self._fresh_tmp()
        self._emit_line(f"{m} = load ptr, ptr {ptr_slot}")
        key_val = self._emit_expr(key_arg)
        if key_val is None:
            return None
        raw = self._fresh_tmp()
        self._emit_line(f"{raw} = call i64 @nyet_map_get(ptr {m}, ptr {key_val}, i64 0)")
        return self._from_i64_slot(raw, val_ty)

    def _emit_map_set(self, name: str, key_arg: N.Expr, value: N.Expr | None) -> str | None:
        self._declare_extern("declare void @nyet_map_set(ptr, ptr, i64)")
        val_ty = self._env_map_val_ty[name]
        ptr_slot, _ = self._env[name]
        m = self._fresh_tmp()
        self._emit_line(f"{m} = load ptr, ptr {ptr_slot}")
        key_val = self._emit_expr(key_arg)
        if key_val is None or value is None:
            return None
        val = self._emit_expr(value)
        if val is None:
            return None
        v_slot = self._to_i64_slot(val, val_ty)
        self._emit_line(f"call void @nyet_map_set(ptr {m}, ptr {key_val}, i64 {v_slot})")
        return None

    # ------------------------------------------------------------------
    # dyn Trait objects — a 2-word fat pointer {data, vtable}
    # ------------------------------------------------------------------

    def _get_or_register_dyn_type(self, trait_name: str) -> str:
        """Return the synthesized fat-pointer struct type name for a
        trait, registering it (as a plain 2-field struct so the existing
        GEP machinery applies) on first use."""
        if trait_name in self._dyn_types:
            return self._dyn_types[trait_name]
        name = f"dyn.{trait_name}"
        self._structs[name] = [("data", "ptr"), ("vtable", "ptr")]
        self._struct_type_lines.append(f"%{name} = type {{ ptr, ptr }}")
        self._dyn_types[trait_name] = name
        return name

    def _get_or_register_vtable(self, concrete_type: str, trait_name: str) -> str | None:
        """Return the global vtable constant for (concrete_type, trait),
        synthesizing it on first use from `_method_impls`. Returns None
        if `concrete_type` doesn't implement every method of the trait."""
        key = (concrete_type, trait_name)
        if key in self._vtables:
            return self._vtables[key]
        methods = self._traits.get(trait_name, [])
        fn_ptrs: list[str] = []
        for m in methods:
            mangled = self._method_impls.get((concrete_type, m.name))
            if mangled is None:
                return None
            fn_ptrs.append(f"ptr @{self._dyn_entry(mangled)}")
        name = f"@vtable.{concrete_type}.{trait_name}"
        n = len(fn_ptrs)
        self._struct_type_lines.append(f"{name} = constant [{n} x ptr] [{', '.join(fn_ptrs)}]")
        self._vtables[key] = name
        return name

    def _dyn_entry(self, mangled: str) -> str:
        """The function a vtable slot points at for impl method `mangled`.

        A call through a vtable passes `self` as the fat pointer's data
        pointer. A method on a scalar type (`impl Display i32`) takes `self` by
        value, so it gets a thunk that loads the value from that pointer first
        -- see `_emit_dyn_coerce`, which boxes scalars."""
        sig = self._fn_sigs.get(mangled)
        if sig is None or not sig[0] or sig[0][0] == "ptr":
            return mangled
        param_tys, ret_ty = sig
        thunk = f"{mangled}.dyn"
        if thunk not in self._synth_fns:
            self_ty, rest = param_tys[0], param_tys[1:]
            params = ", ".join(["ptr %self"] + [f"{t} %a{i}" for i, t in enumerate(rest)])
            args = ", ".join([f"{self_ty} %v"] + [f"{t} %a{i}" for i, t in enumerate(rest)])
            if ret_ty == "void":
                call = [f"  call void @{mangled}({args})", "  ret void"]
            else:
                call = [f"  %r = call {ret_ty} @{mangled}({args})", f"  ret {ret_ty} %r"]
            self._synth_fns[thunk] = "\n".join(
                [
                    f"define internal {ret_ty} @{thunk}({params}) {{",
                    "entry:",
                    f"  %v = load {self_ty}, ptr %self",
                    *call,
                    "}",
                    "",
                ]
            )
        return thunk

    def _emit_dyn_coerce(self, arg_node: N.Expr, trait_name: str) -> str | None:
        """Coerce a concrete value (typically `&some_struct`) into a
        `dyn Trait` fat pointer for passing to a dyn-typed parameter."""
        inner = self._unwrap_borrow(arg_node)
        concrete_val = self._emit_expr(inner)
        if concrete_val is None:
            return None
        concrete_ty = self._infer_nyet_type_name(inner)
        if concrete_ty is None and isinstance(inner, N.Ident):
            concrete_ty = self._env_struct_name.get(inner.name)
        if concrete_ty is None:
            # A primitive, e.g. main.no's `(log_value &42)`.
            concrete_ty = self._infer_nyet_type_from_arg(inner)
        vtable_name = self._get_or_register_vtable(concrete_ty, trait_name)
        if vtable_name is None:
            return None
        value_ty = self._infer_llvm_type(inner)
        if value_ty != "ptr":
            # Box a scalar so the fat pointer's data field is a pointer; the
            # vtable's thunk loads it back -- see `_dyn_entry`.
            self._declare_extern("declare ptr @malloc(i64)")
            box = self._fresh_tmp()
            self._emit_line(f"{box} = call ptr @malloc(i64 8)")
            self._emit_line(f"store {value_ty} {concrete_val}, ptr {box}")
            concrete_val = box

        dyn_struct = self._get_or_register_dyn_type(trait_name)
        fat_ptr = self._heap_alloc_struct(dyn_struct, 16)
        data_field = self._fresh_tmp()
        self._emit_line(
            f"{data_field} = getelementptr inbounds %{dyn_struct}, ptr {fat_ptr}, i32 0, i32 0"
        )
        self._emit_line(f"store ptr {concrete_val}, ptr {data_field}")
        vt_field = self._fresh_tmp()
        self._emit_line(
            f"{vt_field} = getelementptr inbounds %{dyn_struct}, ptr {fat_ptr}, i32 0, i32 1"
        )
        self._emit_line(f"store ptr {vtable_name}, ptr {vt_field}")
        return fat_ptr

    def _dyn_method_llvm_sig(self, trait_name: str, method_name: str) -> tuple[list[str], str]:
        """LLVM signature for a trait method, treating Self (and &Self)
        as ptr -- the calling convention every impl actually shares,
        since structs always pass by pointer regardless of ownership."""
        for m in self._traits.get(trait_name, []):
            if m.name != method_name:
                continue
            arg_tys = []
            for p in m.params:
                inner = p.type.inner if isinstance(p.type, N.RefType) else p.type
                arg_tys.append("ptr" if isinstance(inner, N.SelfType) else self._llvm_type(p.type))
            ret = m.return_type
            ret_ty = "ptr" if isinstance(ret, N.SelfType) else self._llvm_ret_type(ret)
            return arg_tys, ret_ty
        return [], "void"

    def _emit_dyn_call(
        self, dyn_name: str, trait_name: str, method_name: str, args: list[N.Expr]
    ) -> str | None:
        """Dispatch a trait method call through a `dyn Trait` binding's
        vtable: load {data, vtable} from the fat pointer, index the
        vtable by the method's declared position in the trait, and
        issue an indirect call passing `data` as the first (self) arg."""
        methods = self._traits.get(trait_name, [])
        names = [m.name for m in methods]
        if method_name not in names:
            return None
        method_idx = names.index(method_name)

        dyn_struct = self._get_or_register_dyn_type(trait_name)
        ptr_slot, _ = self._env[dyn_name]
        fat_ptr = self._fresh_tmp()
        self._emit_line(f"{fat_ptr} = load ptr, ptr {ptr_slot}")

        data_field = self._fresh_tmp()
        self._emit_line(
            f"{data_field} = getelementptr inbounds %{dyn_struct}, ptr {fat_ptr}, i32 0, i32 0"
        )
        data_ptr = self._fresh_tmp()
        self._emit_line(f"{data_ptr} = load ptr, ptr {data_field}")

        vt_field = self._fresh_tmp()
        self._emit_line(
            f"{vt_field} = getelementptr inbounds %{dyn_struct}, ptr {fat_ptr}, i32 0, i32 1"
        )
        vt_ptr = self._fresh_tmp()
        self._emit_line(f"{vt_ptr} = load ptr, ptr {vt_field}")

        n = len(methods)
        slot_ptr = self._fresh_tmp()
        self._emit_line(
            f"{slot_ptr} = getelementptr inbounds [{n} x ptr], "
            f"ptr {vt_ptr}, i32 0, i32 {method_idx}"
        )
        fn_ptr = self._fresh_tmp()
        self._emit_line(f"{fn_ptr} = load ptr, ptr {slot_ptr}")

        arg_tys, ret_ty = self._dyn_method_llvm_sig(trait_name, method_name)
        # First arg is `self` -- the data pointer, not `args[0]` (which is
        # the dyn-typed receiver expression itself, e.g. `val` in
        # `(display val)`; its concrete value lives in the fat pointer).
        call_arg_tys = ["ptr"]
        call_arg_vals = [data_ptr]
        for i, a in enumerate(args[1:], start=1):
            v = self._emit_expr(a)
            if v is None:
                continue
            call_arg_tys.append(arg_tys[i] if i < len(arg_tys) else self._infer_llvm_type(a))
            call_arg_vals.append(v)

        args_str = ", ".join(f"{t} {v}" for t, v in zip(call_arg_tys, call_arg_vals, strict=False))
        if ret_ty == "void":
            self._emit_line(f"call void {fn_ptr}({args_str})")
            return None
        tmp = self._fresh_tmp()
        self._emit_line(f"{tmp} = call {ret_ty} {fn_ptr}({args_str})")
        return tmp

    # ------------------------------------------------------------------
    # User-defined function calls
    # ------------------------------------------------------------------

    def _emit_user_call(self, name: str, args: list[N.Expr]) -> str | None:
        dyn_traits = self._fn_param_dyn_traits.get(name)
        array_elems = self._fn_param_array_elem.get(name)
        mut_refs = self._fn_param_mut_ref.get(name)
        arg_vals: list[tuple[str, str]] = []
        for i, arg in enumerate(args):
            if mut_refs and i < len(mut_refs) and mut_refs[i] is not None:
                arg_vals.append(("ptr", self._emit_place_ptr(arg)))
                continue
            trait_name = dyn_traits[i] if dyn_traits and i < len(dyn_traits) else None
            if trait_name is not None:
                v = self._emit_dyn_coerce(arg, trait_name)
                if v is None:
                    # This used to drop the argument silently, calling the
                    # function with too few arguments (a crash at runtime).
                    raise NotImplementedError(
                        f"codegen: can't pass the argument at {arg.span} as a `dyn "
                        f"{trait_name}` -- its type doesn't implement every method of "
                        f"{trait_name}"
                    )
                arg_vals.append(("ptr", v))
                continue
            elem_ty = array_elems[i] if array_elems and i < len(array_elems) else None
            if elem_ty is not None:
                v = self._emit_expr_as_array(arg, elem_ty)
                ty = "ptr"
            else:
                v = self._emit_expr(arg)
                ty = self._infer_llvm_type(arg)
            if v is not None:
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
        closure = self._fresh_tmp()
        self._emit_line(f"{closure} = load ptr, ptr {ptr}")

        arg_tys: list[str] = []
        arg_vals: list[str] = []
        for i, arg in enumerate(args):
            v = self._emit_expr(arg)
            if v is None:
                continue
            arg_tys.append(sig_types[i] if i < len(sig_types) else self._infer_llvm_type(arg))
            arg_vals.append(v)
        return self._emit_indirect_call_raw(closure, arg_tys, arg_vals, ret_type)

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
