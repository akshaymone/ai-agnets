"""
tree-sitter Java parser wrapper.

Provides a thin, reusable interface over tree-sitter so every analyzer
can obtain a parsed tree and run S-expression queries without touching
tree-sitter internals directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

from tree_sitter import Language, Node, Parser, Tree

# tree-sitter-java ships a Python extension that exposes the compiled grammar
import tree_sitter_java as _tsjava

logger = logging.getLogger(__name__)


class JavaParser:
    """Wraps the tree-sitter Java grammar for convenient Java source parsing."""

    def __init__(self) -> None:
        self.language: Language = Language(_tsjava.language())
        self._parser: Parser = Parser(self.language)

    # ── Public API ────────────────────────────────────────────────────────────

    def parse_file(self, file_path: str | Path) -> Tuple[Tree, bytes]:
        """
        Parse a Java source file.

        Returns
        -------
        tree   : tree-sitter Tree (AST root)
        source : raw file bytes (needed for node text extraction)
        """
        path = Path(file_path)
        source = path.read_bytes()
        tree = self._parser.parse(source)
        if tree.root_node.has_error:
            logger.warning("Parse errors detected in %s — results may be incomplete", path)
        return tree, source

    def parse_bytes(self, source: bytes) -> Tuple[Tree, bytes]:
        """Parse Java source given as raw bytes (useful in tests)."""
        tree = self._parser.parse(source)
        return tree, source

    def node_text(self, node: Node, source: bytes) -> str:
        """Extract the source text covered by *node*."""
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def query(self, pattern: str):
        """
        Compile and return a tree-sitter Query for this language.

        Parameters
        ----------
        pattern : S-expression query string
        """
        return self.language.query(pattern)
