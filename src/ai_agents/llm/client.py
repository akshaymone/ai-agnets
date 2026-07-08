"""
LangChain-based LLM client for enhancing API call data.

Design principles
─────────────────
• Backend is Ollama by default (local, no API key needed).
• Swapping to any other LangChain-compatible LLM requires only changing
  the `build_llm()` factory — all callers stay the same.
• The LLM is used *only* when static analysis is insufficient:
  - Dynamic/variable-based URIs
  - Ambiguous body content types
  - Missing path parameter names
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── LLM factory ───────────────────────────────────────────────────────────────

def build_llm(
    provider: str = "ollama",
    model: str = "llama3.2",
    base_url: str = "http://localhost:11434",
    **kwargs: Any,
):
    """
    Build and return a LangChain BaseLLM instance.

    Parameters
    ----------
    provider : "ollama" (default) | "openai" | "anthropic" | ...
    model    : model name understood by the provider
    base_url : only used for providers that accept a custom endpoint (Ollama)
    kwargs   : extra keyword arguments forwarded to the LLM constructor

    Returns
    -------
    A LangChain BaseLLM / BaseChatModel instance.
    """
    provider = provider.lower()

    if provider == "ollama":
        from langchain_ollama import OllamaLLM
        return OllamaLLM(model=model, base_url=base_url, **kwargs)

    if provider == "openai":
        from langchain_openai import ChatOpenAI  # type: ignore
        return ChatOpenAI(model=model, **kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # type: ignore
        return ChatAnthropic(model=model, **kwargs)

    raise ValueError(
        f"Unknown LLM provider: {provider!r}. "
        "Supported: 'ollama', 'openai', 'anthropic'."
    )


# ── Prompt templates ──────────────────────────────────────────────────────────

_URI_RESOLUTION_PROMPT = """\
You are a Java code analysis assistant. Analyze the following Java code snippet that uses java.net.http.HttpClient.

The static analyzer was unable to determine the exact REST API URL because it is dynamically constructed.

Raw URI expression found in code:
  {raw_uri_expr}

Surrounding code context:
```java
{context_code}
```

Your task:
1. Determine the most likely URL path template (use {{paramName}} for path parameters).
2. List all path parameter names.
3. List any query parameter names you can identify.
4. Identify the base URL host if visible.

Respond ONLY with valid JSON in this exact format (no markdown, no explanation):
{{
  "url_template": "/path/{{paramName}}",
  "base_url": "https://api.example.com",
  "path_params": ["paramName"],
  "query_params": {{"key": "example_value"}},
  "confidence": "high|medium|low",
  "notes": "any additional observations"
}}
"""

_BODY_ENHANCEMENT_PROMPT = """\
You are a Java code analysis assistant. Analyze this Java code that makes an HTTP request:

```java
{context_code}
```

The request body expression is:
  {body_expr}

Determine:
1. The most likely Content-Type (application/json, application/x-www-form-urlencoded, text/plain, etc.)
2. A JSON Schema snippet describing the body structure (if detectable).

Respond ONLY with valid JSON (no markdown):
{{
  "content_type": "application/json",
  "body_schema": {{}},
  "notes": "any observations"
}}
"""


# ── LLM client ────────────────────────────────────────────────────────────────

class LLMClient:
    """
    Wraps a LangChain LLM to enhance API call data that static analysis
    could not fully resolve.

    Usage
    -----
    client = LLMClient()                              # Ollama + llama3.2
    client = LLMClient(provider="openai", model="gpt-4o")
    result = client.resolve_dynamic_uri(raw_expr, context_code)
    """

    def __init__(
        self,
        provider: str = "ollama",
        model: str = "llama3.2",
        base_url: str = "http://localhost:11434",
        **kwargs: Any,
    ) -> None:
        self.provider = provider
        self.model = model
        self._llm = build_llm(provider=provider, model=model, base_url=base_url, **kwargs)
        logger.info("LLMClient initialized: provider=%s model=%s", provider, model)

    def resolve_dynamic_uri(
        self,
        raw_uri_expr: str,
        context_code: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Ask the LLM to interpret a dynamic/variable-based URI expression.

        Returns a dict with keys: url_template, base_url, path_params,
        query_params, confidence, notes — or None on failure.
        """
        prompt = _URI_RESOLUTION_PROMPT.format(
            raw_uri_expr=raw_uri_expr,
            context_code=context_code[:2000],  # keep prompt manageable
        )
        return self._invoke_json(prompt)

    def enhance_body(
        self,
        body_expr: str,
        context_code: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Ask the LLM to infer body content type and schema from source context.

        Returns a dict with keys: content_type, body_schema, notes — or None.
        """
        prompt = _BODY_ENHANCEMENT_PROMPT.format(
            body_expr=body_expr,
            context_code=context_code[:2000],
        )
        return self._invoke_json(prompt)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _invoke_json(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Invoke LLM and parse JSON response."""
        try:
            raw = self._llm.invoke(prompt)
            # BaseLLM returns str; ChatModel returns AIMessage
            text = raw if isinstance(raw, str) else raw.content
            return _parse_json_response(text)
        except Exception as exc:
            logger.warning("LLM invocation failed: %s", exc)
            return None


def _parse_json_response(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM response text (handles markdown fences)."""
    # Strip ```json ... ``` fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    logger.debug("Could not parse LLM JSON response: %s", text[:200])
    return None
