"""
ai_agents.main
==============
Parses outgoing Java HTTP client calls from a Java source file using
Tree-sitter, then uses a LangChain LLM (Gemini or Ollama) with
structured output to generate an OpenAPI 3.1.0 endpoint specification.

Usage
-----
    LLM_PROVIDER=gemini GOOGLE_API_KEY=<key> run-agent [path/to/JavaFile.java]

Environment variables
---------------------
    LLM_PROVIDER    : 'gemini' (default) or 'ollama'
    GOOGLE_API_KEY  : required when LLM_PROVIDER=gemini
    OLLAMA_BASE_URL : optional; defaults to http://localhost:11434
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Literal, Optional

# ── Tree-sitter ────────────────────────────────────────────────────────────────
import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

# ── Pydantic ───────────────────────────────────────────────────────────────────
from pydantic import BaseModel, Field

# ── LangChain ──────────────────────────────────────────────────────────────────
from langchain_core.prompts import ChatPromptTemplate

# ---------------------------------------------------------------------------
# 1.  Tree-sitter setup
# ---------------------------------------------------------------------------

JAVA_LANGUAGE = Language(tsjava.language())

_parser: Parser | None = None


def _get_parser() -> Parser:
    global _parser
    if _parser is None:
        _parser = Parser(JAVA_LANGUAGE)
    return _parser


# ---------------------------------------------------------------------------
# 2.  AST Extraction helpers
# ---------------------------------------------------------------------------

# Method names that typically represent outgoing HTTP calls
_HTTP_METHOD_NAMES = {
    "send", "execute", "get", "post", "put", "patch", "delete",
    "newCall", "request", "call", "perform", "invoke",
}

# Variable name fragments that suggest a URL
_URL_HINTS = {"url", "uri", "endpoint", "target", "path", "host", "address"}


def _node_text(node: Node, source: bytes) -> str:
    """Return the raw source text for a node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_nodes_by_type(root: Node, node_type: str) -> list[Node]:
    """BFS walk collecting all nodes of a given type."""
    results: list[Node] = []
    stack = [root]
    while stack:
        current = stack.pop()
        if current.type == node_type:
            results.append(current)
        stack.extend(current.children)
    return results


def _ancestor_of_type(node: Node, target_type: str) -> Node | None:
    """Walk up the parent chain looking for a node of target_type."""
    current = node.parent
    while current is not None:
        if current.type == target_type:
            return current
        current = current.parent
    return None


def _is_http_invocation(node: Node, source: bytes) -> bool:
    """
    Heuristic: decide if a method_invocation node represents an HTTP call.
    Checks for known HTTP method names and URL-like string literals nearby.
    """
    # Grab the method name (first named child is usually the method identifier)
    for child in node.children:
        if child.type in ("identifier", "field_access"):
            name = _node_text(child, source).lower()
            if any(m in name for m in _HTTP_METHOD_NAMES):
                return True
    return False


def _extract_url_assignments(method_node: Node, source: bytes) -> list[str]:
    """
    Inside a method_declaration, find local variable assignments whose name
    looks URL-related (e.g. sTargetURL, uri, endpoint) and return their text.
    """
    results: list[str] = []
    for var_decl in _find_nodes_by_type(method_node, "local_variable_declaration"):
        for declarator in _find_nodes_by_type(var_decl, "variable_declarator"):
            # First child of declarator is the variable name
            name_node = declarator.children[0] if declarator.children else None
            if name_node and name_node.type == "identifier":
                var_name = _node_text(name_node, source).lower()
                if any(hint in var_name for hint in _URL_HINTS):
                    results.append(_node_text(var_decl, source))
    return results


def _extract_header_calls(method_node: Node, source: bytes) -> list[str]:
    """
    Find method_invocation nodes inside the method whose name suggests
    setting a header or parameter (e.g. addHeader, setHeader, addParam).
    """
    header_hints = {"header", "param", "query", "cookie", "accept", "content-type"}
    results: list[str] = []
    for inv in _find_nodes_by_type(method_node, "method_invocation"):
        for child in inv.children:
            if child.type in ("identifier", "field_access"):
                name = _node_text(child, source).lower()
                if any(h in name for h in header_hints):
                    results.append(_node_text(inv, source))
                    break
    return results


def extract_http_context(java_source: str) -> list[dict]:
    """
    Parse *java_source* and return a list of context dictionaries, one per
    detected outgoing HTTP call, each containing:
        - method_name   : enclosing Java method name
        - method_code   : full source of the enclosing method_declaration
        - url_hints     : variable assignments that look URL-related
        - header_hints  : invocations that look like header/param setters
    """
    source_bytes = java_source.encode("utf-8")
    tree = _get_parser().parse(source_bytes)
    root = tree.root_node

    contexts: list[dict] = []
    # Deduplicate using the node's byte span — stable across tree-sitter Node instances
    seen_spans: set[tuple[int, int]] = set()

    for inv_node in _find_nodes_by_type(root, "method_invocation"):
        if not _is_http_invocation(inv_node, source_bytes):
            continue

        method_node = _ancestor_of_type(inv_node, "method_declaration")
        if method_node is None:
            continue
        span = (method_node.start_byte, method_node.end_byte)
        if span in seen_spans:
            continue
        seen_spans.add(span)

        # Resolve the enclosing Java method name
        method_name = "<unknown>"
        for child in method_node.children:
            if child.type == "identifier":
                method_name = _node_text(child, source_bytes)
                break

        contexts.append(
            {
                "method_name": method_name,
                "method_code": _node_text(method_node, source_bytes),
                "url_hints": _extract_url_assignments(method_node, source_bytes),
                "header_hints": _extract_header_calls(method_node, source_bytes),
            }
        )

    return contexts


# ---------------------------------------------------------------------------
# 3.  Pydantic schema — strict OpenAPI 3.1.0 endpoint representation
# ---------------------------------------------------------------------------


class OAParameter(BaseModel):
    """A single OpenAPI parameter (query / header / path / cookie)."""

    name: str = Field(description="Parameter name")
    location: Literal["query", "header", "path", "cookie"] = Field(
        alias="in", description="Where the parameter is sent"
    )
    required: bool = Field(default=False, description="Whether the parameter is required")
    description: Optional[str] = Field(default=None, description="Short description")
    schema_type: Literal["string", "integer", "number", "boolean", "array", "object"] = Field(
        default="string",
        alias="schema",
        description="JSON Schema type for this parameter",
    )

    model_config = {"populate_by_name": True}


class OARequestBody(BaseModel):
    """Simplified OpenAPI requestBody."""

    description: Optional[str] = None
    required: bool = True
    content_type: str = Field(
        default="application/json",
        description="Media type, e.g. application/json",
    )
    schema_description: Optional[str] = Field(
        default=None,
        description="Plain-language description of the request body schema",
    )


class OpenAPIEndpoint(BaseModel):
    """
    Strict OpenAPI 3.1.0 representation of a single HTTP endpoint extracted
    from a Java HTTP client method.
    """

    summary: str = Field(description="One-line human-readable summary of what this call does")
    method: Literal["get", "post", "put", "patch", "delete", "head", "options"] = Field(
        description="HTTP method in lowercase"
    )
    path: str = Field(
        description=(
            "The URL path, e.g. /api/v1/users/{userId}. "
            "Use path-parameter placeholders in {braces}. "
            "Strip the scheme and host."
        )
    )
    parameters: List[OAParameter] = Field(
        default_factory=list,
        description="List of query, header, path, or cookie parameters",
    )
    request_body: Optional[OARequestBody] = Field(
        default=None,
        description="Request body if the method sends one (POST/PUT/PATCH)",
    )
    response_description: str = Field(
        default="Successful response",
        description="Short description of the expected success response",
    )


# ---------------------------------------------------------------------------
# 4.  LLM factory
# ---------------------------------------------------------------------------


def get_llm():
    """
    Return a LangChain chat model based on the LLM_PROVIDER env var.

    LLM_PROVIDER=ollama  → ChatOllama(model="llama3", temperature=0)
    LLM_PROVIDER=gemini  → ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)
    (default)            → gemini
    """
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower().strip()

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        print(f"[LLM] Using Ollama (llama3) @ {base_url}", file=sys.stderr)
        return ChatOllama(model="llama3", temperature=0, base_url=base_url)

    # Default: Gemini
    from langchain_google_genai import ChatGoogleGenerativeAI

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GOOGLE_API_KEY environment variable is required for the Gemini provider."
        )
    print("[LLM] Using Google Gemini (gemini-2.5-flash)", file=sys.stderr)
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        google_api_key=api_key,
    )


# ---------------------------------------------------------------------------
# 5.  AI Execution — structured output via LangChain
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert software architect specialising in REST API documentation.
Your task is to analyse a Java HTTP client method and produce a precise
OpenAPI 3.1.0 endpoint specification for the outgoing HTTP call it makes.

Rules:
- Infer the HTTP method from the Java code (e.g. HttpRequest.newBuilder() + POST → post).
- Infer the path from any URL variable found in the code. Strip the scheme and host.
  If the full URL is static, set the path to the path component only.
  Represent dynamic segments as {{paramName}} placeholders.
- List every query string key and every explicitly set header as a parameter.
- If the method sends a body, populate request_body.
- Be concise but accurate. Do NOT invent fields that are not supported by evidence in the code.
"""

_HUMAN_TEMPLATE = """\
Java method name : {method_name}

--- Java source ---
{method_code}

--- URL-related variable assignments found in this method ---
{url_hints}

--- Header / parameter setter calls found in this method ---
{header_hints}
---

Analyse the code above and return the OpenAPI 3.1.0 endpoint specification.
"""


def analyse_http_method(context: dict) -> OpenAPIEndpoint:
    """
    Feed one extracted Java method context to the LLM and return a validated
    OpenAPIEndpoint Pydantic object.
    """
    llm = get_llm()
    structured_llm = llm.with_structured_output(OpenAPIEndpoint)

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _SYSTEM_PROMPT),
            ("human", _HUMAN_TEMPLATE),
        ]
    )

    chain = prompt | structured_llm

    result: OpenAPIEndpoint = chain.invoke(
        {
            "method_name": context["method_name"],
            "method_code": context["method_code"],
            "url_hints": "\n".join(context["url_hints"]) or "(none found)",
            "header_hints": "\n".join(context["header_hints"]) or "(none found)",
        }
    )
    return result


# ---------------------------------------------------------------------------
# 6.  Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """
    CLI entry point.

    Usage:
        run-agent [path/to/JavaFile.java]

    If no path is given, defaults to 'SampleHttpClient.java' in the cwd.
    """
    java_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("SampleHttpClient.java")

    if not java_file.exists():
        print(f"[ERROR] File not found: {java_file}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Parsing Java file: {java_file}", file=sys.stderr)
    java_source = java_file.read_text(encoding="utf-8")

    contexts = extract_http_context(java_source)
    if not contexts:
        print("[WARN] No outgoing HTTP calls detected in the file.", file=sys.stderr)
        sys.exit(0)

    print(f"[INFO] Found {len(contexts)} HTTP method(s) to analyse.", file=sys.stderr)

    openapi_paths: dict = {}

    for ctx in contexts:
        print(f"\n[INFO] Analysing method: {ctx['method_name']} …", file=sys.stderr)
        try:
            endpoint: OpenAPIEndpoint = analyse_http_method(ctx)
        except Exception as exc:
            print(f"[ERROR] LLM call failed for {ctx['method_name']}: {exc}", file=sys.stderr)
            continue

        # Build an OpenAPI paths entry
        path_item = openapi_paths.setdefault(endpoint.path, {})
        operation: dict = {
            "summary": endpoint.summary,
            "parameters": [
                {
                    "name": p.name,
                    "in": p.location,
                    "required": p.required,
                    "description": p.description,
                    "schema": {"type": p.schema_type},
                }
                for p in endpoint.parameters
            ],
            "responses": {
                "200": {"description": endpoint.response_description}
            },
        }
        if endpoint.request_body:
            operation["requestBody"] = {
                "description": endpoint.request_body.description,
                "required": endpoint.request_body.required,
                "content": {
                    endpoint.request_body.content_type: {
                        "schema": {
                            "type": "object",
                            "description": endpoint.request_body.schema_description,
                        }
                    }
                },
            }
        path_item[endpoint.method] = operation

    openapi_doc = {
        "openapi": "3.1.0",
        "info": {
            "title": f"Generated from {java_file.name}",
            "version": "1.0.0",
        },
        "paths": openapi_paths,
    }

    print(json.dumps(openapi_doc, indent=2))


if __name__ == "__main__":
    main()
