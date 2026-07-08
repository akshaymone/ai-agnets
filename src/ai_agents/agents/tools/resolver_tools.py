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
    """

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
        entry = index.lookup_symbol(name, context_file or None)
        if entry is None:
            return json.dumps({
                "found": False,
                "name": name,
                "message": f"Symbol '{name}' not found in the project index.",
            })
        result = entry.to_dict()
        result["found"] = True
        logger.debug("[tool:lookup_symbol] %s → %s", name, entry.value)
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
        source = index.get_class_source(class_name)
        if source is None:
            return f"Class '{class_name}' not found in the project. Available classes: {', '.join(list(index._class_files.keys())[:20])}"
        # Truncate very large classes to keep context manageable
        if len(source) > 8000:
            source = source[:8000] + "\n... [truncated for brevity]"
        logger.debug("[tool:get_class_source] %s → %d chars", class_name, len(source))
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
        value = index.lookup_property(key)
        if value is None:
            return json.dumps({
                "found": False,
                "key": key,
                "message": f"Property '{key}' not found in any .properties or .yml file.",
            })
        logger.debug("[tool:lookup_property] %s → %s", key, value)
        return json.dumps({"found": True, "key": key, "value": value})

    return [lookup_symbol, get_class_source, lookup_property]
