# Nyet compiler development commands. Run `just` to list them.

default:
    @just --list

# Run every test harness
test-all: test-lexer test-parser test-sema test-codegen

# Run the lexer golden tests
test-lexer:
    python3 tests/lexer/run.py

# Run the parser golden tests
test-parser:
    python3 tests/parser/run.py

# Run the sema golden tests
test-sema:
    python3 tests/sema/run.py

# Run the codegen golden tests
test-codegen:
    python3 tests/codegen/run.py

# Lex a .no file and print its tokens
lex FILE:
    python3 -m pynyet.driver lex {{FILE}}

# Parse a .no file and print its AST
parse FILE:
    python3 -m pynyet.driver parse {{FILE}}

# Type-check a .no file
check FILE:
    python3 -m pynyet.driver check {{FILE}}

# Compile a .no file to a native binary
build FILE:
    python3 -m pynyet.driver build {{FILE}}

# Compile and run a .no file
run FILE:
    python3 -m pynyet.driver run {{FILE}}

# Compile the C runtime to build/*.o
runtime:
    mkdir -p build
    for src in runtime/*.c; do \
        name=$(basename "$src" .c); \
        cc -Wall -Wextra -std=c11 -Iruntime -c "$src" -o "build/$name.o"; \
    done

# Remove generated files and caches
clean:
    rm -rf build output.ll output
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
