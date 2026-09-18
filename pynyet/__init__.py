"""Nyet compiler package.

The public API is deliberately small while the new pipeline is under
construction. Import specific modules directly:

    from pynyet.source import SourceFile
    from pynyet.lexer.scanner import lex
"""

# Single source of truth for the compiler's version. The release
# workflow (.github/workflows/windows-package.yml) reads this to name
# the release tag in the public NeonSupernova/nyet-releases repo, so
# cutting a release means bumping this line -- nothing else.
__version__ = "0.4.1"
