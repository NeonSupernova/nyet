"""PyInstaller entry point for the standalone `nyet` CLI.

Not meant to be run directly from a checkout -- use
`python3 -m pynyet.driver` for that. This only exists as the script
PyInstaller's Analysis() bundles into nyet.exe.
"""

import sys

from pynyet.driver import main

if __name__ == "__main__":
    sys.exit(main())
