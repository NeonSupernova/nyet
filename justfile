# Nyet compiler development commands. Run `just` to list them.

default:
    @just --list

# Run every golden-file test harness plus the pytest unit suite
test-all: test-lexer test-parser test-sema test-codegen unit-test

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

# Run the pytest unit test suite (tests/unit/)
unit-test:
    python3 -m pytest tests/unit/

# Format the compiler source with ruff
fmt:
    python3 -m ruff format pynyet/

# Check formatting without modifying files
fmt-check:
    python3 -m ruff format --check pynyet/

# Lint the compiler source with ruff
lint:
    python3 -m ruff check pynyet/

# Type-check the compiler source with mypy
typecheck:
    python3 -m mypy

# Combined coverage: pytest unit tests + all four golden harnesses
# (the golden harnesses exercise far more of the compiler than the
# unit tests alone, so this is the meaningful number, not `pytest --cov`)
coverage:
    rm -f .coverage
    python3 -m coverage run -m pytest tests/unit/
    python3 -m coverage run -a tests/lexer/run.py
    python3 -m coverage run -a tests/parser/run.py
    python3 -m coverage run -a tests/sema/run.py
    python3 -m coverage run -a tests/codegen/run.py
    python3 -m coverage report

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
