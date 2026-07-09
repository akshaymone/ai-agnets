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

        logger.debug(
            "[ChainDetector] ══════════════════════════════════════════"
        )
        logger.debug(
            "[ChainDetector] START detect_file: %s", path.name
        )
        logger.debug(
            "[ChainDetector] ══════════════════════════════════════════"
        )

        # Step 1: Parse the file into a tree-sitter AST + raw bytes
        logger.debug(
            "[ChainDetector] STEP 1 — Parsing '%s' with tree-sitter into an AST...",
            path.name,
        )
        tree, source = self._parser.parse_file(path)
        logger.debug(
            "[ChainDetector]   → AST root node type: '%s', source size: %d bytes",
            tree.root_node.type,
            len(source),
        )

        # Step 2: Quick fingerprint check — skip files that cannot have HttpClient
        logger.debug(
            "[ChainDetector] STEP 2 — Fingerprint check: looking for %s in raw bytes",
            [fp.decode() for fp in _FINGERPRINTS],
        )
        matched_fingerprint = next(
            (fp.decode() for fp in _FINGERPRINTS if fp in source), None
        )
        if matched_fingerprint is None:
            logger.debug(
                "[ChainDetector]   → No HttpClient fingerprint found. Skipping file (fast exit)."
            )
            return []
        logger.debug(
            "[ChainDetector]   → Fingerprint matched: '%s'. Will scan full AST.",
            matched_fingerprint,
        )

        # Step 3: Extract file-level context (imports + package) once
        logger.debug(
            "[ChainDetector] STEP 3 — Extracting imports and package declaration..."
        )
        imports = _extract_imports(tree.root_node, source)
        package = _extract_package(tree.root_node, source)
        logger.debug(
            "[ChainDetector]   → Package: '%s', Imports found: %d",
            package or "<none>",
            len(imports),
        )
        for imp in imports[:5]:
            logger.debug("[ChainDetector]     import: %s", imp.strip())
        if len(imports) > 5:
            logger.debug("[ChainDetector]     ... and %d more", len(imports) - 5)

        # Step 4: Walk AST and collect all HttpRequest.newBuilder()…build() chains
        logger.debug(
            "[ChainDetector] STEP 4 — Traversing AST tree to find all .build() chains..."
        )
        chains: List[RawChain] = []
        self._traverse(tree.root_node, source, str(path), imports, package, chains)

        logger.info(
            "[ChainDetector] %s → %d chain(s) found", path.name, len(chains)
        )
        for i, ch in enumerate(chains, 1):
            logger.debug(
                "[ChainDetector]   Chain %d/%d: class=%s line=%d method=%s uri_expr='%s'",
                i,
                len(chains),
                ch.class_name,
                ch.line,
                ch.suspected_method or "GET",
                ch.raw_uri_expr or "<not extracted>",
            )

        return chains

    def detect_bytes(self, source: bytes, file_path: str = "<bytes>") -> List[RawChain]:
        """Parse Java source from bytes (useful for tests)."""
        logger.debug("[ChainDetector] detect_bytes: parsing %d bytes from '%s'", len(source), file_path)
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
        """
        Recursively walk every AST node.

        When we find a method_invocation node whose method name is 'build'
        AND whose full text contains 'newBuilder' + 'HttpRequest', we know
        we found a complete HttpRequest builder chain.

        We do NOT recurse into the matched chain node — it's fully consumed.
        """
        logger.debug(
            "[ChainDetector]   _traverse: node_type='%s' @ bytes[%d:%d]",
            node.type,
            node.start_byte,
            node.end_byte,
        )

        if _is_http_request_build(node, source):
            logger.debug(
                "[ChainDetector]   *** MATCH: Found HttpRequest.newBuilder()...build() node at line %d ***",
                node.start_point[0] + 1,
            )
            chain = self._build_raw_chain(node, source, file_path, imports, package)
            if chain:
                results.append(chain)
                logger.debug(
                    "[ChainDetector]   Added chain: %s (total so far: %d)",
                    chain.summary(),
                    len(results),
                )
            else:
                logger.debug(
                    "[ChainDetector]   Chain node matched but _build_raw_chain returned None"
                )
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
        """
        Given the AST node for the .build() call, extract all context needed
        for the LLM to resolve the full API call.
        """
        logger.debug(
            "[ChainDetector]   _build_raw_chain: extracting context from build node..."
        )

        # Extract the raw source text of the builder chain (everything from
        # HttpRequest.newBuilder(…) up to .build())
        chain_text = source[build_node.start_byte: build_node.end_byte].decode(
            "utf-8", errors="replace"
        )
        logger.debug(
            "[ChainDetector]     chain_text (%d chars):\n%s",
            len(chain_text),
            chain_text[:500] + ("..." if len(chain_text) > 500 else ""),
        )

        # Walk UP the AST to find the enclosing class_declaration
        logger.debug("[ChainDetector]     Finding enclosing class (walking up AST)...")
        class_name, class_body = _find_enclosing_class(build_node, source)
        logger.debug(
            "[ChainDetector]     → Enclosing class: '%s' (%d chars of body)",
            class_name,
            len(class_body),
        )

        # Walk UP the AST to find the enclosing method_declaration or constructor_declaration.
        # This gives the LLM the local variable assignments ABOVE the builder chain
        # (e.g. String endpoint = BASE_URL + "/users/" + userId;)
        logger.debug(
            "[ChainDetector]     Finding enclosing method (walking up AST)..."
        )
        method_context = _find_enclosing_method(build_node, source)
        if method_context:
            logger.debug(
                "[ChainDetector]     → Enclosing method found (%d chars). "
                "LLM will see local variables defined above the chain.",
                len(method_context),
            )
        else:
            logger.debug(
                "[ChainDetector]     → No enclosing method found (chain may be at field level)."
            )

        # Quick text-mining hints: try to extract URI expression and HTTP method
        # WITHOUT resolving anything — just regex on the chain text
        logger.debug(
            "[ChainDetector]     Extracting URI hint and HTTP method hint from chain text..."
        )
        raw_uri_expr = _hint_uri_expr(chain_text)
        suspected_method = _hint_http_method(chain_text)
        logger.debug(
            "[ChainDetector]     → raw_uri_expr hint: '%s'", raw_uri_expr or "<not found>"
        )
        logger.debug(
            "[ChainDetector]     → suspected HTTP method: '%s'", suspected_method
        )

        chain = RawChain(
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
        logger.debug(
            "[ChainDetector]     → RawChain built: %s", chain.summary()
        )
        return chain


# ── AST helpers ───────────────────────────────────────────────────────────────

def _is_http_request_build(node: Node, source: bytes) -> bool:
    """
    True if node is a .build() call anchored to an HttpRequest.newBuilder() chain.

    Check logic:
      1. node type must be 'method_invocation' (a method call in Java AST)
      2. the called method name must be 'build'
      3. the full text of the node must contain 'newBuilder' (anchors us to
         HttpRequest.newBuilder()) AND 'HttpRequest' or 'java.net.http'
    """
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
    is_match = "newBuilder" in full_text and (
        "HttpRequest" in full_text or "java.net.http" in full_text
    )
    if is_match:
        logger.debug(
            "[ChainDetector]   _is_http_request_build: TRUE for method_invocation node "
            "at bytes[%d:%d]",
            node.start_byte,
            node.end_byte,
        )
    return is_match


def _find_enclosing_class(node: Node, source: bytes) -> Tuple[str, str]:
    """
    Walk UP the AST parent chain until we find a class_declaration node.
    Returns (class_name, class_body_source).
    """
    logger.debug(
        "[ChainDetector]   _find_enclosing_class: starting from node type='%s'", node.type
    )
    current = node.parent
    depth = 0
    while current is not None:
        depth += 1
        logger.debug(
            "[ChainDetector]     Walking up (depth=%d): node type='%s'", depth, current.type
        )
        if current.type == "class_declaration":
            # Extract class name — the first 'identifier' child of the class node
            class_name = ""
            for child in current.children:
                if child.type == "identifier":
                    class_name = source[child.start_byte: child.end_byte].decode("utf-8")
                    break
            class_body = source[current.start_byte: current.end_byte].decode(
                "utf-8", errors="replace"
            )
            logger.debug(
                "[ChainDetector]     Found class_declaration at depth=%d: class='%s'",
                depth,
                class_name,
            )
            return class_name, class_body
        current = current.parent
    logger.debug(
        "[ChainDetector]     No class_declaration found after walking %d levels up.", depth
    )
    return "<unknown>", ""


def _find_enclosing_method(node: Node, source: bytes) -> str:
    """
    Walk up the AST to find the enclosing method_declaration and return its
    full source text.  This gives the LLM the local variable assignments that
    appear *above* the builder chain inside the same method — critical for
    tracing variables like `endpoint`, `fullUrl`, `resourcePath`, etc.
    Returns an empty string if no enclosing method is found.

    Why this matters:
    ─────────────────
    Without method_context, the LLM only sees the builder chain itself, e.g.:
        HttpRequest.newBuilder().uri(URI.create(endpoint)).GET().build()

    It sees 'endpoint' but doesn't know what it is. With method_context it sees:
        String endpoint = BASE_TENANT_URL + "/users/" + userId + "/documents";
        ...
        HttpRequest.newBuilder().uri(URI.create(endpoint)).GET().build()

    Now it can trace 'endpoint' back to the local variable definition.
    """
    logger.debug(
        "[ChainDetector]   _find_enclosing_method: starting from node type='%s'", node.type
    )
    current = node.parent
    depth = 0
    while current is not None:
        depth += 1
        logger.debug(
            "[ChainDetector]     Walking up (depth=%d): node type='%s'", depth, current.type
        )
        if current.type in ("method_declaration", "constructor_declaration"):
            method_src = source[current.start_byte: current.end_byte].decode(
                "utf-8", errors="replace"
            )
            logger.debug(
                "[ChainDetector]     Found %s at depth=%d (%d chars)",
                current.type,
                depth,
                len(method_src),
            )
            return method_src
        current = current.parent
    logger.debug(
        "[ChainDetector]     No method/constructor declaration found after %d levels.", depth
    )
    return ""


def _extract_imports(root: Node, source: bytes) -> List[str]:
    """
    Collect all import declaration strings from the file.
    These are at the top level of the AST (direct children of the
    compilation_unit / root node).
    """
    imports: List[str] = []
    for child in root.children:
        if child.type == "import_declaration":
            imports.append(
                source[child.start_byte: child.end_byte].decode("utf-8", errors="replace").strip()
            )
    logger.debug(
        "[ChainDetector]   _extract_imports: found %d import statements", len(imports)
    )
    return imports


def _extract_package(root: Node, source: bytes) -> str:
    """Return the package name declared at the top of the file."""
    for child in root.children:
        if child.type == "package_declaration":
            text = source[child.start_byte: child.end_byte].decode("utf-8", errors="replace")
            m = re.search(r"package\s+([\w.]+)\s*;", text)
            if m:
                pkg = m.group(1)
                logger.debug("[ChainDetector]   _extract_package: '%s'", pkg)
                return pkg
    logger.debug("[ChainDetector]   _extract_package: no package declaration found")
    return ""


# ── Quick-hint extractors (text-level, no resolution) ────────────────────────

def _hint_uri_expr(chain_text: str) -> str:
    """
    Extract the raw URI expression from chain text without resolving it.
    This is just a best-effort text extraction — the LLM will actually resolve it.

    Example: URI.create(BASE_URL + "/health")  →  BASE_URL + "/health"

    We try three patterns:
      1. .uri(URI.create(...))  or  .uri(new URI(...))
      2. .uri(someExpr)  (bare expression)
      3. newBuilder(URI.create(...))  (URI passed directly to newBuilder)
    """
    logger.debug("[ChainDetector]   _hint_uri_expr: scanning chain text for URI expression...")

    # Match .uri(URI.create(...)) or .uri(new URI(...))
    m = re.search(
        r'\.uri\s*\(\s*(?:URI\.create|new\s+URI)\s*\((.+?)\)\s*\)',
        chain_text,
        re.DOTALL,
    )
    if m:
        expr = m.group(1).strip()
        logger.debug("[ChainDetector]     Pattern 1 (URI.create/new URI): '%s'", expr)
        return expr

    # Match .uri(someExpr)
    m = re.search(r'\.uri\s*\((.+?)\)', chain_text, re.DOTALL)
    if m:
        expr = m.group(1).strip()
        logger.debug("[ChainDetector]     Pattern 2 (.uri(expr)): '%s'", expr)
        return expr

    # Match URI passed directly to newBuilder(URI.create(...))
    m = re.search(
        r'newBuilder\s*\(\s*(?:URI\.create|new\s+URI)\s*\((.+?)\)\s*\)',
        chain_text,
        re.DOTALL,
    )
    if m:
        expr = m.group(1).strip()
        logger.debug("[ChainDetector]     Pattern 3 (newBuilder(URI.create(...))): '%s'", expr)
        return expr

    logger.debug("[ChainDetector]     No URI expression found by any pattern.")
    return ""


def _hint_http_method(chain_text: str) -> str:
    """
    Heuristically detect the HTTP method from chain text.

    Looks for:
      - .POST()  .PUT()  .DELETE()  .PATCH()  .HEAD()  .GET()
      - .method("PUT", ...)  (generic method call)
    Defaults to GET if nothing is found (HttpClient default).
    """
    logger.debug("[ChainDetector]   _hint_http_method: scanning for HTTP method...")
    for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD"):
        if f".{method}(" in chain_text or f".{method}()" in chain_text:
            logger.debug("[ChainDetector]     Found explicit method: %s", method)
            return method
    if ".GET()" in chain_text:
        logger.debug("[ChainDetector]     Found explicit method: GET")
        return "GET"
    m = re.search(r'\.method\s*\(\s*"([A-Z]+)"', chain_text)
    if m:
        method = m.group(1)
        logger.debug("[ChainDetector]     Found .method(\"%s\",...)", method)
        return method
    logger.debug("[ChainDetector]     No explicit method found, defaulting to GET")
    return "GET"  # default
