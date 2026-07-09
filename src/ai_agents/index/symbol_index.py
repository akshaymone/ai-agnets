"""
SymbolIndex — pre-scans ALL Java + properties files and builds a lookup index.

Built once before any LLM calls. Every tool call during the LLM resolver loop
queries this in-memory index — no I/O during the agentic loop.

What is indexed
───────────────
Java fields:
  - static final String FOO = "value";
  - final String bar = "value";
  - private static final int TIMEOUT = 30;

Properties files (.properties):
  - api.base-url=https://api.example.com

YAML files (.yml / .yaml):
  - api:
      base-url: https://api.example.com
    → indexed as "api.base-url"

Class → file mapping:
  - ClassName → absolute file path (for get_class_source tool)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from tree_sitter import Node

from ..parsers.java_parser import JavaParser

logger = logging.getLogger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SymbolEntry:
    """A single resolved symbol (field declaration)."""
    name: str
    value: str                  # Literal value as a string
    java_type: str              # "String", "int", "boolean", etc.
    file: str                   # Absolute path
    line: int                   # 1-based
    class_name: str
    is_static: bool
    is_final: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "type": self.java_type,
            "file": self.file,
            "line": self.line,
            "class": self.class_name,
            "is_static": self.is_static,
            "is_final": self.is_final,
        }


@dataclass
class SymbolIndex:
    """
    In-memory index of all resolvable symbols in the project.

    Usage:
        index = SymbolIndexBuilder(java_parser).build(java_files, config_files)
        entry = index.lookup_symbol("BASE_TENANT_URL", context_file="TenantService.java")
        value = index.lookup_property("api.base-url")
        path  = index.class_file("ApiConstants")
    """

    # symbol_name → list of entries (same name can appear in multiple classes)
    _symbols: Dict[str, List[SymbolEntry]] = field(default_factory=dict)

    # class_name → absolute file path
    _class_files: Dict[str, str] = field(default_factory=dict)

    # property key → value  (from .properties / .yml)
    _properties: Dict[str, str] = field(default_factory=dict)

    # ── Public API ────────────────────────────────────────────────────────────

    def lookup_symbol(
        self,
        name: str,
        context_file: Optional[str] = None,
    ) -> Optional[SymbolEntry]:
        """
        Look up a symbol by name.

        Strategy (in priority order):
          1. Same file first (if context_file given) — avoids false matches
             from identically-named constants in different classes
          2. Same class name heuristic (context_file stem matches class name)
          3. Prefer static final entries (most likely to be the intended constant)
          4. First entry in the index as last resort

        Returns the best match, or None if not found.
        """
        logger.debug(
            "[SymbolIndex] lookup_symbol(name='%s', context_file='%s')",
            name,
            context_file or "<none>",
        )
        entries = self._symbols.get(name)
        if not entries:
            logger.debug(
                "[SymbolIndex]   '%s' not in index at all (total unique names: %d)",
                name,
                len(self._symbols),
            )
            return None

        logger.debug(
            "[SymbolIndex]   '%s' found in %d location(s)", name, len(entries)
        )
        for e in entries:
            logger.debug(
                "[SymbolIndex]     candidate: class=%s file=%s line=%d value='%s' "
                "static=%s final=%s",
                e.class_name, e.file, e.line, e.value, e.is_static, e.is_final,
            )

        # Same-file priority
        if context_file:
            ctx = str(Path(context_file).resolve())
            same_file = [e for e in entries if str(Path(e.file).resolve()) == ctx]
            if same_file:
                logger.debug(
                    "[SymbolIndex]   Strategy 1 (same-file) → matched: class=%s value='%s'",
                    same_file[0].class_name,
                    same_file[0].value,
                )
                return same_file[0]

            # Same class name in the context file name (heuristic)
            ctx_stem = Path(context_file).stem
            same_class = [e for e in entries if e.class_name == ctx_stem]
            if same_class:
                logger.debug(
                    "[SymbolIndex]   Strategy 2 (same-class heuristic, stem='%s') → "
                    "matched: value='%s'",
                    ctx_stem,
                    same_class[0].value,
                )
                return same_class[0]

            logger.debug(
                "[SymbolIndex]   Strategies 1+2 failed for context_file='%s'. "
                "Falling back to global strategies.",
                context_file,
            )

        # Prefer static final entries (more likely to be the "constant" intended)
        static_finals = [e for e in entries if e.is_static and e.is_final]
        if static_finals:
            logger.debug(
                "[SymbolIndex]   Strategy 3 (static final) → matched: class=%s value='%s'",
                static_finals[0].class_name,
                static_finals[0].value,
            )
            return static_finals[0]

        logger.debug(
            "[SymbolIndex]   Strategy 4 (first match) → class=%s value='%s'",
            entries[0].class_name,
            entries[0].value,
        )
        return entries[0]

    def lookup_property(self, key: str) -> Optional[str]:
        """Look up a property value by key (from .properties / .yml files)."""
        logger.debug("[SymbolIndex] lookup_property(key='%s')", key)
        value = self._properties.get(key) or self._properties.get(
            key.replace("-", ".").replace("_", ".")
        )
        if value is not None:
            logger.debug("[SymbolIndex]   Found: '%s' = '%s'", key, value)
        else:
            logger.debug("[SymbolIndex]   Not found: '%s'", key)
        return value

    def class_file(self, class_name: str) -> Optional[str]:
        """Return the absolute file path for a given class name."""
        path = self._class_files.get(class_name)
        logger.debug(
            "[SymbolIndex] class_file(class_name='%s') → %s",
            class_name,
            path or "<not found>",
        )
        return path

    def get_class_source(self, class_name: str) -> Optional[str]:
        """Return the full source of a Java class, or None if not found."""
        logger.debug("[SymbolIndex] get_class_source(class_name='%s')", class_name)
        path = self.class_file(class_name)
        if path and Path(path).exists():
            source = Path(path).read_text(encoding="utf-8", errors="replace")
            logger.debug(
                "[SymbolIndex]   → Read %d chars from '%s'", len(source), path
            )
            return source
        logger.debug("[SymbolIndex]   → class '%s' has no file or file missing", class_name)
        return None

    def all_symbol_names(self) -> List[str]:
        """Return a sorted list of all indexed symbol names."""
        return sorted(self._symbols.keys())

    def stats(self) -> dict:
        return {
            "symbols": sum(len(v) for v in self._symbols.values()),
            "classes": len(self._class_files),
            "properties": len(self._properties),
        }

    # ── Internal mutators (used by builder) ───────────────────────────────────

    def _add_symbol(self, entry: SymbolEntry) -> None:
        self._symbols.setdefault(entry.name, []).append(entry)

    def _add_class(self, class_name: str, file_path: str) -> None:
        self._class_files[class_name] = file_path

    def _add_property(self, key: str, value: str) -> None:
        self._properties[key] = value


# ── Builder ───────────────────────────────────────────────────────────────────

class SymbolIndexBuilder:
    """Builds a SymbolIndex by AST-scanning all Java + config files."""

    def __init__(self, java_parser: JavaParser) -> None:
        self._parser = java_parser

    def build(
        self,
        java_files: List[Path],
        config_files: Optional[List[Path]] = None,
    ) -> SymbolIndex:
        """
        Scan all files and return a populated SymbolIndex.

        This runs ONCE before any LLM calls. All lookups during the
        agentic loop are instant dict lookups — no file I/O.

        Parameters
        ----------
        java_files   : list of .java file paths
        config_files : list of .properties / .yml paths (optional)
        """
        logger.info(
            "[SymbolIndex] ══════════════════════════════════════════"
        )
        logger.info(
            "[SymbolIndex] BUILD START: %d Java file(s), %d config file(s)",
            len(java_files),
            len(config_files or []),
        )
        logger.info(
            "[SymbolIndex] Goal: index all static final fields, class names, "
            "and .properties/.yml values for instant lookup during LLM tool calls."
        )
        index = SymbolIndex()

        logger.debug("[SymbolIndex] --- Indexing Java files ---")
        for i, jf in enumerate(java_files, 1):
            logger.debug(
                "[SymbolIndex] [%d/%d] Indexing Java: %s",
                i, len(java_files), jf.name,
            )
            try:
                self._index_java_file(jf, index)
            except Exception as exc:
                logger.warning("[SymbolIndex] Failed to index %s: %s", jf, exc)

        logger.debug("[SymbolIndex] --- Indexing config files ---")
        for i, cf in enumerate(config_files or [], 1):
            logger.debug(
                "[SymbolIndex] [%d/%d] Indexing config: %s",
                i, len(config_files or []), cf.name,
            )
            try:
                self._index_config_file(cf, index)
            except Exception as exc:
                logger.warning("[SymbolIndex] Failed to index config %s: %s", cf, exc)

        stats = index.stats()
        logger.info(
            "[SymbolIndex] BUILD COMPLETE: %d symbols, %d classes, %d properties",
            stats["symbols"], stats["classes"], stats["properties"],
        )
        logger.debug(
            "[SymbolIndex] All indexed symbol names: %s",
            index.all_symbol_names(),
        )
        logger.debug(
            "[SymbolIndex] All indexed class names: %s",
            sorted(index._class_files.keys()),
        )
        logger.debug(
            "[SymbolIndex] All indexed property keys: %s",
            sorted(index._properties.keys()),
        )
        logger.info(
            "[SymbolIndex] ══════════════════════════════════════════"
        )
        return index

    # ── Java indexing ─────────────────────────────────────────────────────────

    def _index_java_file(self, path: Path, index: SymbolIndex) -> None:
        """
        Parse a single Java file and extract all classes and fields.

        Uses tree-sitter to build an AST, then walks it recursively.
        Only literal-valued fields are indexed (method call results are skipped).
        """
        logger.debug("[SymbolIndex] _index_java_file: parsing '%s'...", path.name)
        tree, source = self._parser.parse_file(path)
        root = tree.root_node
        logger.debug(
            "[SymbolIndex]   AST root type='%s', source=%d bytes",
            root.type, len(source),
        )
        before_symbols = sum(len(v) for v in index._symbols.values())
        before_classes = len(index._class_files)
        self._walk_for_classes(root, source, str(path), index)
        after_symbols = sum(len(v) for v in index._symbols.values())
        after_classes = len(index._class_files)
        logger.debug(
            "[SymbolIndex]   '%s' → added %d symbol(s), %d class(es)",
            path.name,
            after_symbols - before_symbols,
            after_classes - before_classes,
        )

    def _walk_for_classes(
        self,
        node: Node,
        source: bytes,
        file_path: str,
        index: SymbolIndex,
        parent_class: str = "",
    ) -> None:
        """
        Recursively walk AST, extracting class names and field declarations.

        When we find a class/interface/enum node:
          1. Register class_name → file_path in the class index
          2. Walk the class body for field_declaration nodes
          3. Recurse for nested classes
        """
        if node.type in ("class_declaration", "interface_declaration", "enum_declaration"):
            class_name = _node_identifier(node, source) or parent_class
            if class_name:
                index._add_class(class_name, file_path)
                logger.debug(
                    "[SymbolIndex]   Registered class: '%s' → %s",
                    class_name,
                    file_path,
                )
            # Walk into class body for fields
            for child in node.children:
                self._walk_for_fields(child, source, file_path, class_name, index)
            # Also recurse for nested classes
            for child in node.children:
                self._walk_for_classes(child, source, file_path, index, class_name)
        else:
            for child in node.children:
                self._walk_for_classes(child, source, file_path, index, parent_class)

    def _walk_for_fields(
        self,
        node: Node,
        source: bytes,
        file_path: str,
        class_name: str,
        index: SymbolIndex,
    ) -> None:
        """
        Extract field_declaration nodes and index their literal values.

        Only fields with simple literal initializers are indexed:
          static final String BASE_URL = "https://api.example.com";  ← indexed
          static HttpClient client = HttpClient.newHttpClient();      ← skipped (not literal)
        """
        if node.type == "field_declaration":
            field_text = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace").strip()
            logger.debug(
                "[SymbolIndex]   field_declaration in class '%s': %s",
                class_name,
                field_text[:100].replace("\n", " "),
            )
            entry = _parse_field_declaration(node, source, file_path, class_name)
            if entry:
                index._add_symbol(entry)
                logger.debug(
                    "[SymbolIndex]     ✓ Indexed symbol: '%s' = '%s' (type=%s, static=%s, final=%s)",
                    entry.name,
                    entry.value,
                    entry.java_type,
                    entry.is_static,
                    entry.is_final,
                )
            else:
                logger.debug(
                    "[SymbolIndex]     ✗ Skipped (no simple literal value found)"
                )
            return

        for child in node.children:
            if child.type not in ("class_declaration", "interface_declaration"):
                self._walk_for_fields(child, source, file_path, class_name, index)

    # ── Config indexing ───────────────────────────────────────────────────────

    def _index_config_file(self, path: Path, index: SymbolIndex) -> None:
        suffix = path.suffix.lower()
        if suffix == ".properties":
            self._index_properties(path, index)
        elif suffix in (".yml", ".yaml"):
            self._index_yaml(path, index)

    def _index_properties(self, path: Path, index: SymbolIndex) -> None:
        """
        Parse a .properties file and add every key=value pair to the index.
        Lines starting with # or ! are comments and are skipped.
        """
        logger.debug("[SymbolIndex] _index_properties: '%s'", path.name)
        text = path.read_text(encoding="utf-8", errors="replace")
        count = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                k, v = key.strip(), value.strip()
                index._add_property(k, v)
                logger.debug("[SymbolIndex]   .properties: '%s' = '%s'", k, v)
                count += 1
            elif ":" in line:
                key, _, value = line.partition(":")
                k, v = key.strip(), value.strip()
                index._add_property(k, v)
                logger.debug("[SymbolIndex]   .properties: '%s' = '%s'", k, v)
                count += 1
        logger.debug(
            "[SymbolIndex]   '%s' → indexed %d property/ies", path.name, count
        )

    def _index_yaml(self, path: Path, index: SymbolIndex) -> None:
        """
        Flatten YAML into dotted keys without requiring PyYAML.

        Example:
            api:
              base-url: https://example.com
        → Indexed as: 'api.base-url' = 'https://example.com'

        The indentation stack tracks nesting level so we can build
        the full dotted key path for each leaf value.
        """
        logger.debug("[SymbolIndex] _index_yaml: '%s'", path.name)
        text = path.read_text(encoding="utf-8", errors="replace")
        prefix_stack: List[tuple] = []  # (indent, key_prefix)
        count = 0
        for raw_line in text.splitlines():
            if not raw_line.strip() or raw_line.strip().startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip())
            stripped = raw_line.strip()

            # Pop stack for dedent
            while prefix_stack and prefix_stack[-1][0] >= indent:
                popped = prefix_stack.pop()
                logger.debug(
                    "[SymbolIndex]   YAML dedent: popped '%s' (was at indent %d)",
                    popped[1], popped[0],
                )

            if ":" in stripped:
                key_part, _, val_part = stripped.partition(":")
                key_part = key_part.strip()
                val_part = val_part.strip()

                current_prefix = ".".join(k for _, k in prefix_stack)
                full_key = f"{current_prefix}.{key_part}" if current_prefix else key_part

                if val_part and not val_part.startswith("{") and not val_part.startswith("["):
                    # Leaf value — index it
                    clean_val = val_part.strip("'\"")
                    index._add_property(full_key, clean_val)
                    logger.debug(
                        "[SymbolIndex]   YAML leaf: '%s' = '%s'", full_key, clean_val
                    )
                    count += 1
                else:
                    # Nested key — push to stack for child resolution
                    prefix_stack.append((indent, key_part))
                    logger.debug(
                        "[SymbolIndex]   YAML nesting: pushing '%s' (indent=%d)",
                        key_part, indent,
                    )
        logger.debug(
            "[SymbolIndex]   '%s' → indexed %d YAML property/ies", path.name, count
        )


# ── AST parsing helpers ───────────────────────────────────────────────────────

def _node_identifier(node: Node, source: bytes) -> Optional[str]:
    """Return the first identifier child of a node (usually the class/field name)."""
    for child in node.children:
        if child.type == "identifier":
            return source[child.start_byte: child.end_byte].decode("utf-8")
    return None


def _parse_field_declaration(
    node: Node,
    source: bytes,
    file_path: str,
    class_name: str,
) -> Optional[SymbolEntry]:
    """
    Parse a field_declaration AST node and extract name + literal value.

    Returns None if the value is not a simple literal (e.g., method call result).
    """
    full_text = source[node.start_byte: node.end_byte].decode("utf-8", errors="replace")

    # Detect modifiers
    is_static = "static" in full_text.split("=")[0]
    is_final = "final" in full_text.split("=")[0]

    # Extract type
    java_type = _extract_java_type(node, source)

    # Extract declarator (name = value)
    name: Optional[str] = None
    value: Optional[str] = None
    line: int = node.start_point[0] + 1

    for child in node.children:
        if child.type == "variable_declarator":
            for vchild in child.children:
                if vchild.type == "identifier" and name is None:
                    name = source[vchild.start_byte: vchild.end_byte].decode("utf-8")
                elif vchild.type in (
                    "string_literal",
                    "decimal_integer_literal",
                    "decimal_floating_point_literal",
                    "true",
                    "false",
                ):
                    raw = source[vchild.start_byte: vchild.end_byte].decode("utf-8")
                    # Strip surrounding quotes from string literals
                    if raw.startswith('"') and raw.endswith('"'):
                        value = raw[1:-1]
                    else:
                        value = raw
                elif vchild.type == "string_literal":
                    raw = source[vchild.start_byte: vchild.end_byte].decode("utf-8")
                    value = raw.strip('"')

    if name and value is not None:
        return SymbolEntry(
            name=name,
            value=value,
            java_type=java_type,
            file=file_path,
            line=line,
            class_name=class_name,
            is_static=is_static,
            is_final=is_final,
        )

    return None


def _extract_java_type(node: Node, source: bytes) -> str:
    """Extract the declared Java type from a field_declaration node."""
    for child in node.children:
        if child.type in (
            "type_identifier",
            "integral_type",
            "floating_point_type",
            "boolean_type",
            "void_type",
        ):
            return source[child.start_byte: child.end_byte].decode("utf-8")
        if child.type == "generic_type":
            return source[child.start_byte: child.end_byte].decode("utf-8")
    return "unknown"
