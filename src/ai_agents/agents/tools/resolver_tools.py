"""
LangChain tools available to the LLM Resolver agent.

Each tool is a pure function that queries the SymbolIndex (no I/O).
The SymbolIndex is pre-built before the agentic loop starts.

Tools
─────
  lookup_symbol(name, context_file)  →  symbol declaration + value
  get_class_source(class_name)       →  full source of a Java class
  lookup_property(key)               →  value from .properties/.yml

The LLM calls these tools when it needs more information to resolve
a URL, header value, or other reference in the builder chain.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_core.tools import tool

from ...index.symbol_index import SymbolIndex

logger = logging.getLogger(__name__)


def make_tools(index: SymbolIndex) -> list:
    """
    Create the tool list bound to a specific SymbolIndex instance.

    LangChain tools must be pure functions (no self), so we use closures
    to inject the index dependency.

    Why closures?
    ─────────────
    The @tool decorator wraps a plain function. We can't pass `index` as a
    parameter because LangChain auto-generates the tool schema from the function
    signature and would expose 'index' to the LLM. So we close over it instead.
    """
    logger.debug(
        "[make_tools] Creating tools bound to SymbolIndex "
        "(symbols=%d, classes=%d, properties=%d)",
        sum(len(v) for v in index._symbols.values()),
        len(index._class_files),
        len(index._properties),
    )

    @tool
    def lookup_symbol(name: str, context_file: str = "") -> str:
        """
        Look up the value of a Java field or constant by its name.

        Use this when you see a variable reference in the builder chain
        (e.g. BASE_TENANT_URL, API_BASE_URL, HOST) and need its value.

        Args:
            name: The field/constant name exactly as written in code (e.g. "BASE_TENANT_URL")
            context_file: The Java source file where the reference appears (helps prioritise same-class fields)

        Returns:
            JSON string with: name, value, type, file, line, class, is_static, is_final.
            Returns a "not found" message if the symbol is unknown.
        """
        logger.info(
            "[tool:lookup_symbol] ► LLM called lookup_symbol(name='%s', context_file='%s')",
            name,
            context_file or "<not provided>",
        )
        logger.debug(
            "[tool:lookup_symbol]   Strategy: 1) same file → 2) same class name → "
            "3) static final → 4) first match"
        )

        entry = index.lookup_symbol(name, context_file or None)

        if entry is None:
            logger.info(
                "[tool:lookup_symbol]   ✗ Symbol '%s' NOT found in index. "
                "All indexed symbols: %s",
                name,
                index.all_symbol_names()[:20],
            )
            result = {
                "found": False,
                "name": name,
                "message": f"Symbol '{name}' not found in the project index.",
            }
            logger.debug("[tool:lookup_symbol]   Returning: %s", result)
            return json.dumps(result)

        result = entry.to_dict()
        result["found"] = True
        logger.info(
            "[tool:lookup_symbol]   ✓ Found: '%s' = '%s' (type=%s, class=%s, file=%s, line=%d, "
            "static=%s, final=%s)",
            entry.name,
            entry.value,
            entry.java_type,
            entry.class_name,
            entry.file,
            entry.line,
            entry.is_static,
            entry.is_final,
        )
        logger.debug(
            "[tool:lookup_symbol]   Returning to LLM: %s", json.dumps(result)
        )
        return json.dumps(result)

    @tool
    def get_class_source(class_name: str) -> str:
        """
        Return the full Java source code of a class by its simple class name.

        Use this when you need broader context about how a class is structured
        (e.g. to understand what fields or methods it has).

        Args:
            class_name: Simple class name (e.g. "TenantService", "ApiConstants")

        Returns:
            The full Java source as a string, or an error message if not found.
        """
        logger.info(
            "[tool:get_class_source] ► LLM called get_class_source(class_name='%s')",
            class_name,
        )

        # Log what classes ARE available (helps debug "not found" cases)
        available = list(index._class_files.keys())
        logger.debug(
            "[tool:get_class_source]   Available classes in index (%d): %s",
            len(available),
            available[:20],
        )

        source = index.get_class_source(class_name)
        if source is None:
            logger.warning(
                "[tool:get_class_source]   ✗ Class '%s' NOT found in index. "
                "Available: %s",
                class_name,
                ", ".join(available[:20]),
            )
            return (
                f"Class '{class_name}' not found in the project. "
                f"Available classes: {', '.join(available[:20])}"
            )

        # Truncate very large classes to keep context manageable
        original_len = len(source)
        if len(source) > 8000:
            source = source[:8000] + "\n... [truncated for brevity]"
            logger.debug(
                "[tool:get_class_source]   Source truncated from %d to 8000 chars to fit LLM context",
                original_len,
            )

        logger.info(
            "[tool:get_class_source]   ✓ Returning source for '%s' (%d chars, file: %s)",
            class_name,
            len(source),
            index._class_files.get(class_name, "?"),
        )
        return source

    @tool
    def lookup_property(key: str) -> str:
        """
        Look up a value from .properties or .yml configuration files.

        Use this when you see @Value("${some.property.key}") or similar
        Spring/config references in the code.

        Args:
            key: The property key (e.g. "api.base-url", "service.host")

        Returns:
            The property value as a string, or a "not found" message.
        """
        logger.info(
            "[tool:lookup_property] ► LLM called lookup_property(key='%s')",
            key,
        )
        logger.debug(
            "[tool:lookup_property]   Total properties in index: %d",
            len(index._properties),
        )

        value = index.lookup_property(key)

        if value is None:
            # Log available properties (useful for debugging missing keys)
            all_keys = list(index._properties.keys())
            logger.warning(
                "[tool:lookup_property]   ✗ Property '%s' NOT found. "
                "Available keys (%d): %s",
                key,
                len(all_keys),
                all_keys[:20],
            )
            result = {
                "found": False,
                "key": key,
                "message": f"Property '{key}' not found in any .properties or .yml file.",
            }
            return json.dumps(result)

        logger.info(
            "[tool:lookup_property]   ✓ Found: '%s' = '%s'",
            key,
            value,
        )
        result = {"found": True, "key": key, "value": value}
        logger.debug(
            "[tool:lookup_property]   Returning to LLM: %s", json.dumps(result)
        )
        return json.dumps(result)

    tools = [lookup_symbol, get_class_source, lookup_property]
    logger.debug(
        "[make_tools] Created %d tools: %s",
        len(tools),
        [t.name for t in tools],
    )
    return tools
