# Nyet Test Harness

The Nyet compiler uses golden-file testing: each compiler phase has its
own directory under `tests/` containing `.no` source fixtures and
committed reference outputs. A phase runner dumps a canonical
representation for each fixture and diffs it against the stored golden.

## Workflow

1. Drop a new `<name>.no` source file into the relevant phase directory.
2. Run `python3 tests/<phase>/run.py`. The first run writes the golden
   file next to the source; commit it alongside the `.no` fixture.
3. On subsequent runs, the harness diffs fresh output against the
   committed golden and fails (exit 1) with a unified diff on mismatch.

## Directory layout

- `tests/lexer/` — lexing. Goldens use the `.tokens` extension.
- `tests/parser/` — parsing (v0.2). Goldens use `.golden`.
- `tests/sema/` — name resolution, type checking, and borrow checking
  (v0.3). Goldens use `.golden`.
- `tests/codegen/` — end-to-end IR / binary behaviour (v0.4). Goldens
  use `.golden`.

Only the lexer harness has real fixtures today; the other runners print
`no cases yet` and exit 0 until their respective milestones land.

`tests/func.no` and `tests/if.no` at the top of `tests/` are legacy
loose fixtures left over from the earlier prototype. They are not part
of any current harness and can be ignored.

## Updating goldens after an intentional change

After changing compiler output on purpose, regenerate the goldens:

```
python3 tests/<phase>/run.py --update
```

Review the resulting diff with `git diff` and commit the new goldens.

## Running specific cases

Pass case names (the file stem, without extension) as positional args:

```
python3 tests/lexer/run.py hello numerics
python3 tests/parser/run.py case1 case2
```
