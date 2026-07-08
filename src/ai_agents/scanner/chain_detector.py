"""
ChainDetector — extracts raw HttpClient builder chains from Java source.

Responsibility
──────────────
Detect every HttpRequest.newBuilder()…build() chain in a Java file and
return a RawChain dataclass containing:
  - the chain code text
  - the enclosing class body (for LLM context)
  - import statements (for cross-file symbol resolution)
  - the file path and start line

Resolution is intentionally NOT done here — the LLM Resolver handles that.
This module is purely an extraction layer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tree_sitter import Node

from ..parsers.java_parser import JavaParser

logger = logging.getLogger(__name__)

# Signals that the file likely uses java.net.http.HttpClient
_FINGERPRINTS = (b"HttpRequest", b"HttpClient", b"java.net.http")


@dataclass
class RawChain:
    """A single detected HttpClient builder chain, ready for LLM resolution."""

    # Location
    file: str
    line: int                       # 1-based start line of the .build() call

    # Code context
    chain_text: str                 # The builder chain source text
    class_name: str                 # Enclosing Java class name
    class_body: str                 # Full body of the enclosing class (for LLM)
    method_context: str             # Full body of the enclosing method (for LLM)
    imports: List[str]              # Import statements from the file
    package: str                    # Package declaration (for cross-file lookup)

    # Hints extracted without LLM (help the LLM start faster)
    raw_uri_expr: str = ""          # e.g.  BASE_URL + "/system/health"
    suspected_method: str = ""      # e.g.  "GET" — may be empty if using .method()

    def summary(self) -> str:
        """Short human-readable description for logging."""
        return f"{Path(self.file).name}:{self.line} [{self.class_name}]"


class ChainDetector:
    """
    Detects all HttpRequest builder chains in a Java file.

    Uses tree-sitter for AST-level detection (not regex), so it handles
    multiline chains, arbitrary indentation, and complex method orderings.
    """

    def __init__(self, java_parser: JavaParser) -> None:
        self._parser = java_parser

    # ── Public API ────────────────────────────────────────────────────────────

    def detect_file(self, file_path: str | Path) -> List[RawChain]:
        """Parse *file_path* and return all detected HttpClient chains."""
        path = Path(file_path)
        tree, source = self._parser.parse_file(path)

        if not any(fp in source for fp in _FINGERPRINTS):
            return []

        # Extract file-level context once
        imports = _extract_imports(tree.root_node, source)
        package = _extract_package(tree.root_node, source)

        chains: List[RawChain] = []
        self._traverse(tree.root_node, source, str(path), imports, package, chains)

        logger.info("[ChainDetector] %s → %d chain(s) found", path.name, len(chains))
        return chains

    def detect_bytes(self, source: bytes, file_path: str = "<bytes>") -> List[RawChain]:
        """Parse Java source from bytes (useful for tests)."""
        tree, source = self._parser.parse_bytes(source)
        imports = _extract_imports(tree.root_node, source)
        package = _extract_package(tree.root_node, source)
        chains: List[RawChain] = []
        self._traverse(tree.root_node, source, file_path, imports, package, chains)
        return chains

    # ── AST traversal ─────────────────────────────────────────────────────────

    def _traverse(
        self,
        node: Node,
        source: bytes,
        file_path: str,
        imports: List[str],
        package: str,
        results: List[RawChain],
    ) -> None:
        if _is_http_request_build(node, source):
            chain = self._build_raw_chain(node, source, file_path, imports, package)
            if chain:
                results.append(chain)
            return  # Don't recurse into this chain — we've consumed it

        for child in node.children:
            self._traverse(child, source, file_path, imports, package, results)

    def _build_raw_chain(
        self,
        build_node: Node,
        source: bytes,
        file_path: str,
        imports: List[str],
        package: str,
    ) -> Optional[RawChain]:
        chain_text = source[build_node.start_byte: build_node.end_byte].decode(
            "utf-8", errors="replace"
        )

        # Find enclosing class
        class_name, class_body = _find_enclosing_class(build_node, source)

        # Find enclosing method — gives the LLM local variable context
        method_context = _find_enclosing_method(build_node, source)

        # Quick pre-extraction hints (no resolution — just text mining)
        raw_uri_expr = _hint_uri_expr(chain_text)
        suspected_method = _hint_http_method(chain_text)

        return RawChain(
            file=file_path,
            line=build_node.start_point[0] + 1,
            chain_text=chain_text,
            class_name=class_name,
            class_body=class_body,
            method_context=method_context,
            imports=imports,
            package=package,
            raw_uri_expr=raw_uri_expr,
            suspected_method=suspected_method,
        )


# ── AST helpers ───────────────────────────────────────────────────────────────

def _is_http_request_build(node: Node, source: bytes) -> bool:
    """True if node is a .build() call anchored to an HttpRequest.newBuilder() chain."""
    if node.type != "method_invocation":
        return False

    method_name: Optional[str] = None
    for child in node.children:
        if child.type == "identifier":
            method_name = source[child.start_byte: child.end_byte].decode("utf-8")
            break

    if method_name != "build":
        return False

    full_text = source[node.start_byte: node.end_byte].decode("utf-8", errors="replace")
    return "newBuilder" in full_text and (
        "HttpRequest" in full_text or "java.net.http" in full_text
    )


def _find_enclosing_class(node: Node, source: bytes) -> Tuple[str, str]:
    """Walk up the AST to find the enclosing class_declaration."""
    current = node.parent
    while current is not None:
        if current.type == "class_declaration":
            # Extract class name
            class_name = ""
            for child in current.children:
                if child.type == "identifier":
                    class_name = source[child.start_byte: child.end_byte].decode("utf-8")
                    break
            class_body = source[current.start_byte: current.end_byte].decode(
                "utf-8", errors="replace"
            )
            return class_name, class_body
        current = current.parent
    return "<unknown>", ""


def _find_enclosing_method(node: Node, source: bytes) -> str:
    """
    Walk up the AST to find the enclosing method_declaration and return its
    full source text.  This gives the LLM the local variable assignments that
    appear *above* the builder chain inside the same method — critical for
    tracing variables like `endpoint`, `fullUrl`, `resourcePath`, etc.
    Returns an empty string if no enclosing method is found.
    """
    current = node.parent
    while current is not None:
        if current.type in ("method_declaration", "constructor_declaration"):
            return source[current.start_byte: current.end_byte].decode(
                "utf-8", errors="replace"
            )
        current = current.parent
    return ""


def _extract_imports(root: Node, source: bytes) -> List[str]:
    """Collect all import declaration strings from the file."""
    imports: List[str] = []
    for child in root.children:
        if child.type == "import_declaration":
            imports.append(
                source[child.start_byte: child.end_byte].decode("utf-8", errors="replace").strip()
            )
    return imports


def _extract_package(root: Node, source: bytes) -> str:
    """Return the package name declared at the top of the file."""
    for child in root.children:
        if child.type == "package_declaration":
            text = source[child.start_byte: child.end_byte].decode("utf-8", errors="replace")
            m = re.search(r"package\s+([\w.]+)\s*;", text)
            if m:
                return m.group(1)
    return ""


# ── Quick-hint extractors (text-level, no resolution) ────────────────────────

def _hint_uri_expr(chain_text: str) -> str:
    """
    Extract the raw URI expression from chain text without resolving it.
    Example: URI.create(BASE_URL + "/health")  →  BASE_URL + "/health"
    """
    # Match .uri(URI.create(...)) or .uri(new URI(...))
    m = re.search(
        r'\.uri\s*\(\s*(?:URI\.create|new\s+URI)\s*\((.+?)\)\s*\)',
        chain_text,
        re.DOTALL,
    )
    if m:
        return m.group(1).strip()

    # Match .uri(someExpr)
    m = re.search(r'\.uri\s*\((.+?)\)', chain_text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Match URI passed directly to newBuilder(URI.create(...))
    m = re.search(
        r'newBuilder\s*\(\s*(?:URI\.create|new\s+URI)\s*\((.+?)\)\s*\)',
        chain_text,
        re.DOTALL,
    )
    if m:
        return m.group(1).strip()

    return ""


def _hint_http_method(chain_text: str) -> str:
    """Heuristically detect the HTTP method from chain text."""
    for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD"):
        if f".{method}(" in chain_text or f".{method}()" in chain_text:
            return method
    if ".GET()" in chain_text:
        return "GET"
    m = re.search(r'\.method\s*\(\s*"([A-Z]+)"', chain_text)
    if m:
        return m.group(1)
    return "GET"  # default
