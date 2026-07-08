"""
LangGraph-based ResolverGraph — the agentic resolution loop.

Graph structure
───────────────

  ┌──────────┐        ┌─────────────────────┐
  │  START   │──────▶│   resolver_node      │
  └──────────┘        │  (LLM + tools)       │
                      └──────────┬───────────┘
                                 │
                    ┌────────────▼────────────┐
                    │  should_continue?        │
                    │  - tool calls → loop     │
                    │  - no calls   → END      │
                    │  - max hops   → END      │
                    └────────────┬────────────┘
                         loop    │     done
                    ┌────────────┘
                    ▼
              ┌─────────────┐
              │ tools_node  │  (executes tool calls, returns results)
              └──────┬──────┘
                     │
                     └──────────▶ resolver_node  (next hop)

The LLM's final message (when it stops calling tools) is expected to be
a structured JSON block containing the resolved API call fields. This is
parsed by extract_result() and converted to an ApiCall object.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Dict, List, Optional, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from ..models.api_call import ApiCall, ResolutionStatus
from ..scanner.chain_detector import RawChain

logger = logging.getLogger(__name__)

MAX_HOPS_DEFAULT = 6


# ── State ─────────────────────────────────────────────────────────────────────

class ResolverState(TypedDict):
    """LangGraph state for a single chain resolution run."""
    messages: Annotated[Sequence[BaseMessage], add_messages]
    chain: RawChain              # The chain being resolved (read-only)
    hop_count: int               # Incremented each resolver→tools cycle
    max_hops: int                # Hard limit
    result: Optional[dict]       # Set by extract_result when LLM finishes


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a Java API analyzer specialized in java.net.http.HttpClient code.

Your task: analyze a Java HttpClient builder chain and extract a complete API call specification.

You must determine:
1. **Full URL** — host + path. If you see ANY variable or constant (e.g. BASE_URL, API_HOST,
   BASE_TENANT_URL, fullUrl, sTargetURL), you MUST call lookup_symbol() to resolve it BEFORE
   writing your final answer. Never leave a variable name in the URL unresolved.
2. **HTTP method** — GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS
3. **Headers** — all headers as key→value. For dynamic values (method params like `token`),
   use {token} as placeholder.
4. **Query parameters** — from URL or code
5. **Path parameters** — dynamic segments of the URL, represented as {paramName}
6. **Request body** — content + content-type if present
7. **Cookies** — parsed from Cookie header if present

Rules:
- ALWAYS call lookup_symbol() for any identifier used in the URL that is not a plain string literal.
  This includes variables like `fullUrl`, `BASE_TENANT_URL`, `resourcePath`, etc.
- If a value comes from @Value("${key}"), call lookup_property("key").
- If you need the full source of a class to find a field value, call get_class_source("ClassName").
- Method parameters (like userId, token) that are NOT resolvable are represented as {paramName}
  placeholders — this is CORRECT, do not try to look them up.
- Only call lookup_symbol for class-level fields/constants or local variables built in the method;
  do NOT look up method parameters (they appear in the method signature).
- Do NOT invent values. If a value is truly unresolvable after tool lookups, say so in notes.

When you have gathered all available information, respond with ONLY a JSON block in this exact format.
IMPORTANT: Every array/object field below MUST be present. Use [] for empty arrays, {} for empty
objects — NEVER use null for these fields:

```json
{
  "method": "GET",
  "url": "https://api.example.com/v1/users/{userId}",
  "url_template": "/v1/users/{userId}",
  "host": "api.example.com",
  "scheme": "https",
  "path_params": ["userId"],
  "query_params": {"page": "1", "size": "20"},
  "headers": {"Accept": "application/json", "Authorization": "Bearer {token}"},
  "cookies": {},
  "body": null,
  "body_content_type": null,
  "resolution_status": "llm_resolved",
  "notes": ["userId is a method parameter represented as {userId}"]
}
```

resolution_status must be one of:
  - "literal"       → URL was a hardcoded string literal
  - "llm_resolved"  → URL required symbol/property lookups to resolve
  - "partial"       → Some fields could not be resolved (explain in notes)
  - "unresolved"    → Could not determine the URL at all

Always end your response with the JSON block. No other text after the JSON.
"""


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_resolver_graph(llm_with_tools, tools: list):
    """
    Build and compile the LangGraph StateGraph for API call resolution.

    Parameters
    ----------
    llm_with_tools : A LangChain chat model already bound to the tools list
    tools          : The list of tool functions (for ToolNode)
    """
    tool_node = ToolNode(tools)

    def resolver_node(state: ResolverState, config: RunnableConfig) -> dict:
        """LLM reasoning node — calls the LLM and optionally invokes tools."""
        response: AIMessage = llm_with_tools.invoke(state["messages"], config)
        return {
            "messages": [response],
            "hop_count": state.get("hop_count", 0) + 1,
        }

    def should_continue(state: ResolverState) -> str:
        """Decide: loop (call tools) → continue | done → end."""
        last_message = state["messages"][-1]
        hop_count = state.get("hop_count", 0)
        max_hops = state.get("max_hops", MAX_HOPS_DEFAULT)

        # Force-exit if we've exceeded max hops
        if hop_count >= max_hops:
            logger.warning(
                "[ResolverGraph] Max hops (%d) reached for chain at %s:%d",
                max_hops,
                state["chain"].file,
                state["chain"].line,
            )
            return "end"

        # If the LLM made tool calls, route to the tools node
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

        # LLM produced a final response — done
        return "end"

    # Build the graph
    graph = StateGraph(ResolverState)
    graph.add_node("resolver", resolver_node)
    graph.add_node("tools", tool_node)

    graph.add_edge(START, "resolver")
    graph.add_conditional_edges(
        "resolver",
        should_continue,
        {"tools": "tools", "end": END},
    )
    graph.add_edge("tools", "resolver")

    return graph.compile()


# ── Result extraction ─────────────────────────────────────────────────────────

def extract_result(state: ResolverState) -> Optional[ApiCall]:
    """
    Parse the LLM's final message into an ApiCall object.

    The LLM is instructed to end with a ```json ... ``` block.
    We extract and parse that block.
    """
    chain = state["chain"]

    # Find last AI message
    last_ai = None
    for msg in reversed(list(state["messages"])):
        if isinstance(msg, AIMessage):
            last_ai = msg
            break

    if last_ai is None:
        logger.error("[extract_result] No AI message found in state")
        return _fallback_api_call(chain, "No AI message in state")

    content = last_ai.content if isinstance(last_ai.content, str) else str(last_ai.content)

    # Extract JSON block
    data = _parse_json_from_response(content)
    if data is None:
        logger.warning("[extract_result] Could not parse JSON from LLM response for %s", chain.summary())
        return _fallback_api_call(chain, f"LLM response did not contain valid JSON: {content[:200]}")

    # Map resolution_status string to enum
    status_str = data.get("resolution_status", "unresolved")
    try:
        status = ResolutionStatus(status_str)
    except ValueError:
        status = ResolutionStatus.UNRESOLVED

    url = data.get("url") or ""
    # If the LLM returned an empty URL, fall back to the raw expression from
    # the source so the call is never silently dropped by the OpenAPI generator.
    if not url:
        url = chain.raw_uri_expr or ""
        if status not in (ResolutionStatus.UNRESOLVED,):
            status = ResolutionStatus.UNRESOLVED
    url_template = data.get("url_template") or _extract_path(url)

    # Coerce None → correct default types for dict/list fields so that Pydantic
    # does not raise a ValidationError when the LLM emits explicit JSON null values.
    def _as_dict(val) -> dict:
        return val if isinstance(val, dict) else {}

    def _as_list(val) -> list:
        return val if isinstance(val, list) else []

    return ApiCall(
        method=(data.get("method") or "GET").upper(),
        url=url,
        url_template=url_template,
        resolution_status=status,
        path_params=_as_list(data.get("path_params")),
        query_params=_as_dict(data.get("query_params")),
        headers=_as_dict(data.get("headers")),
        cookies=_as_dict(data.get("cookies")),
        body=data.get("body"),
        body_content_type=data.get("body_content_type"),
        source_file=chain.file,
        source_line=chain.line,
        library="java.net.http.HttpClient",
        raw_url_expression=chain.raw_uri_expr,
        notes=_as_list(data.get("notes")),
        llm_metadata={
            "hops": state.get("hop_count", 0),
            "class": chain.class_name,
            "host": data.get("host"),
            "scheme": data.get("scheme"),
        },
    )


def _parse_json_from_response(content: str) -> Optional[dict]:
    """Extract and parse the first JSON block from the LLM response."""
    # Try ```json ... ``` block first
    m = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try bare ```...``` block
    m = re.search(r"```\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding a raw JSON object in the response
    m = re.search(r"(\{[^{}]*\"method\"[^{}]*\})", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return None


def _extract_path(url: str) -> Optional[str]:
    """Extract just the path portion from a URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.path or "/"
    except Exception:
        return url


def _fallback_api_call(chain: RawChain, reason: str) -> ApiCall:
    """Create a minimal ApiCall when parsing fails."""
    return ApiCall(
        method=chain.suspected_method or "GET",
        url=chain.raw_uri_expr or "",
        url_template=None,
        resolution_status=ResolutionStatus.UNRESOLVED,
        source_file=chain.file,
        source_line=chain.line,
        library="java.net.http.HttpClient",
        raw_url_expression=chain.raw_uri_expr,
        notes=[f"Resolution failed: {reason}"],
    )


# ── Initial state builder ─────────────────────────────────────────────────────

def build_initial_state(chain: RawChain, max_hops: int = MAX_HOPS_DEFAULT) -> ResolverState:
    """Build the initial LangGraph state for a single chain."""

    method_section = ""
    if chain.method_context:
        method_section = f"""
=== Enclosing Method (READ THIS FIRST to trace local variables) ===
{chain.method_context}
"""

    human_content = f"""Analyze this Java HttpClient builder chain and resolve the full API call.

File: {chain.file}
Line: {chain.line}
Class: {chain.class_name}
{method_section}
=== Builder Chain (the HttpRequest built inside the method above) ===
{chain.chain_text}

=== Full Class Context (for field/constant lookups) ===
{chain.class_body[:4000] if len(chain.class_body) > 4000 else chain.class_body}

=== Imports ===
{chr(10).join(chain.imports[:30])}

IMPORTANT: If the URI argument is a local variable (e.g. `endpoint`, `fullUrl`, `sTargetURL`),
look at the Enclosing Method above to find its definition BEFORE calling lookup_symbol().
Local variables are defined in the method body, not in the symbol index.
Only call lookup_symbol() for class-level fields (e.g. BASE_TENANT_URL, API_HOST).
Then respond with the JSON block.
"""
    return ResolverState(
        messages=[
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=human_content),
        ],
        chain=chain,
        hop_count=0,
        max_hops=max_hops,
        result=None,
    )
