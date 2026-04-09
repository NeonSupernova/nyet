# Nyet C Runtime

Minimal C runtime supporting Nyet-compiled binaries. Provides the few symbols
that v0.1 codegen needs to emit a working hello-world: allocation wrappers,
an owned UTF-8 string type, and simple stdout I/O.

## Building

From the repository root:

```sh
mkdir -p build
cc -Wall -Wextra -std=c11 -Iruntime -c runtime/alloc.c  -o build/alloc.o
cc -Wall -Wextra -std=c11 -Iruntime -c runtime/string.c -o build/string.o
cc -Wall -Wextra -std=c11 -Iruntime -c runtime/io.c     -o build/io.o
```

Or, to build them all at once into a single object directory:

```sh
cc -Wall -Wextra -std=c11 -Iruntime -c runtime/*.c
```

There are no external library dependencies beyond libc.

## FFI contract

Nyet codegen emits calls to the following symbols. All are declared in
`runtime/nyet_runtime.h` and wrapped in `extern "C"` for future C++ linkage.

### Allocation

| Symbol       | Signature                        | Purpose                                 |
| ------------ | -------------------------------- | --------------------------------------- |
| `nyet_alloc` | `void* nyet_alloc(size_t bytes)` | Allocate `bytes`; aborts on OOM.        |
| `nyet_free`  | `void  nyet_free(void* ptr)`     | Free a pointer previously allocated.    |

### Strings

Nyet strings are owned UTF-8 slices represented by the `nyet_string` struct:

```c
typedef struct {
    char*  data;
    size_t len;
} nyet_string;
```

| Symbol                   | Signature                                         | Purpose                                                |
| ------------------------ | ------------------------------------------------- | ------------------------------------------------------ |
| `nyet_string_from_cstr`  | `nyet_string nyet_string_from_cstr(const char*)`  | Copy a NUL-terminated C string into an owned slice.    |
| `nyet_string_drop`       | `void nyet_string_drop(nyet_string)`              | Free the backing allocation.                           |

### I/O

| Symbol              | Signature                              | Purpose                                    |
| ------------------- | -------------------------------------- | ------------------------------------------ |
| `nyet_println`      | `void nyet_println(const char*)`       | Write a C string plus `\n` to stdout.      |
| `nyet_print_i32`    | `void nyet_print_i32(int32_t)`         | Write a decimal `int32_t` to stdout.       |
| `nyet_print_string` | `void nyet_print_string(nyet_string)`  | Write a Nyet string's bytes to stdout.     |

## TODOs for future milestones

- Reference counting for shared heap values.
- `Shared[T]` atomic refcount primitives.
- Async task scheduler / runtime for `async`/`await`.
- Proper error reporting (panics, backtraces, structured errors) instead of
  `abort()` on allocation failure.
