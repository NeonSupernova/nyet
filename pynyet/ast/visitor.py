"""AST visitor base class.

Dispatches ``visit(node)`` to ``visit_<ClassName>(node)`` if defined,
otherwise falls back to ``generic_visit(node)`` which walks every
dataclass field and recurses into child ``Node`` instances and lists of
nodes.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

from pynyet.ast.nodes import Node


class Visitor:
    """Base class for AST traversal.

    Subclasses override ``visit_<NodeClassName>`` methods as needed.
    """

    def visit(self, node: Any) -> Any:
        if node is None:
            return None
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is not None:
            return method(node)
        return self.generic_visit(node)

    def generic_visit(self, node: Any) -> Any:
        if not isinstance(node, Node) or not is_dataclass(node):
            return None
        for f in fields(node):
            value = getattr(node, f.name, None)
            self._walk(value)
        return None

    def _walk(self, value: Any) -> None:
        if isinstance(value, Node):
            self.visit(value)
        elif isinstance(value, list):
            for item in value:
                self._walk(item)
        elif isinstance(value, tuple):
            for item in value:
                self._walk(item)
