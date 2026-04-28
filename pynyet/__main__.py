"""Allow ``python -m pynyet`` to invoke the compiler driver."""

from pynyet.driver import main

if __name__ == "__main__":
    raise SystemExit(main())
