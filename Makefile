# Nyet compiler development Makefile.
# Run `make help` to see available targets.

.DEFAULT_GOAL := help
.PHONY: help test-lexer test-parser test-sema test-codegen test-all lex check build run runtime clean

help:
	@echo "Nyet dev targets:"
	@echo "  make help            show this message"
	@echo "  make test-lexer      run the lexer golden tests"
	@echo "  make test-parser     run the parser golden tests"
	@echo "  make test-sema       run the sema golden tests (no cases yet)"
	@echo "  make test-codegen    run the codegen golden tests (no cases yet)"
	@echo "  make test-all        run every test harness"
	@echo "  make lex FILE=...    lex a .no file and print its tokens"
	@echo "  make check FILE=...  type-check a .no file"
	@echo "  make build FILE=...  compile a .no file to a native binary"
	@echo "  make run FILE=...    compile and run a .no file"
	@echo "  make runtime         compile the C runtime to build/runtime.o"
	@echo "  make clean           remove generated files and caches"

test-lexer:
	python3 tests/lexer/run.py

test-parser:
	@if [ -f tests/parser/run.py ]; then \
		python3 tests/parser/run.py; \
	else \
		echo "test-parser: tests/parser/run.py not present yet"; \
	fi

test-sema:
	@if [ -f tests/sema/run.py ]; then \
		python3 tests/sema/run.py; \
	else \
		echo "test-sema: tests/sema/run.py not present yet"; \
	fi

test-codegen:
	@if [ -f tests/codegen/run.py ]; then \
		python3 tests/codegen/run.py; \
	else \
		echo "test-codegen: tests/codegen/run.py not present yet"; \
	fi

test-all: test-lexer test-parser test-sema test-codegen

lex:
	@if [ -z "$(FILE)" ]; then \
		echo "usage: make lex FILE=<path>"; \
		exit 1; \
	fi
	python3 -m pynyet.driver lex $(FILE)

check:
	@if [ -z "$(FILE)" ]; then \
		echo "usage: make check FILE=<path>"; \
		exit 1; \
	fi
	python3 -m pynyet.driver check $(FILE)

build:
	@if [ -z "$(FILE)" ]; then \
		echo "usage: make build FILE=<path>"; \
		exit 1; \
	fi
	python3 -m pynyet.driver build $(FILE)

run:
	@if [ -z "$(FILE)" ]; then \
		echo "usage: make run FILE=<path>"; \
		exit 1; \
	fi
	python3 -m pynyet.driver run $(FILE)

runtime:
	@if [ ! -d runtime ]; then \
		echo "runtime: runtime/ directory not present yet"; \
		exit 0; \
	fi; \
	mkdir -p build; \
	found=0; \
	for src in runtime/*.c; do \
		[ -e "$$src" ] || continue; \
		found=1; \
		name=$$(basename "$$src" .c); \
		cc -Wall -Wextra -std=c11 -Iruntime -c "$$src" -o "build/$$name.o"; \
	done; \
	if [ "$$found" = "0" ]; then \
		echo "runtime: no .c sources in runtime/"; \
	fi

clean:
	@rm -rf build output.ll output
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
