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
    logger.debug(
        "[ResolverGraph] Building LangGraph StateGraph with %d tool(s): %s",
        len(tools),
        [t.name for t in tools],
    )

    tool_node = ToolNode(tools)
    logger.debug("[ResolverGraph] ToolNode created (will execute LLM tool calls)")

    def resolver_node(state: ResolverState, config: RunnableConfig) -> dict:
        """
        LLM reasoning node — the core of the agentic loop.

        What happens here:
        ─────────────────
        1. The LLM receives ALL messages accumulated so far (system prompt,
           human question, previous tool results, previous AI responses).
        2. The LLM either:
           (a) Calls one or more tools → returns an AIMessage with tool_calls
           (b) Decides it has enough info → returns a final AIMessage with JSON
        3. We increment hop_count so we can enforce max_hops later.
        """
        hop = state.get("hop_count", 0) + 1
        max_hops = state.get("max_hops", MAX_HOPS_DEFAULT)
        chain = state["chain"]
        messages = state["messages"]

        logger.info(
            "[ResolverGraph] ── HOP %d/%d ── Invoking LLM for: %s",
            hop,
            max_hops,
            chain.summary(),
        )
        logger.debug(
            "[ResolverGraph]   Current message history: %d message(s)",
            len(messages),
        )

        # Log each message type in the current context window
        for i, msg in enumerate(messages):
            msg_type = type(msg).__name__
            if isinstance(msg, SystemMessage):
                logger.debug(
                    "[ResolverGraph]   msg[%d] SystemMessage: <%d chars system prompt>",
                    i,
                    len(msg.content),
                )
            elif isinstance(msg, HumanMessage):
                preview = msg.content[:120].replace("\n", " ") if isinstance(msg.content, str) else str(msg.content)[:120]
                logger.debug(
                    "[ResolverGraph]   msg[%d] HumanMessage: '%s...'", i, preview
                )
            elif isinstance(msg, AIMessage):
                tool_calls_info = (
                    f" [has {len(msg.tool_calls)} tool_call(s)]" if msg.tool_calls else " [no tool calls]"
                )
                preview = msg.content[:120].replace("\n", " ") if isinstance(msg.content, str) else ""
                logger.debug(
                    "[ResolverGraph]   msg[%d] AIMessage%s: '%s...'",
                    i,
                    tool_calls_info,
                    preview,
                )
            elif isinstance(msg, ToolMessage):
                preview = msg.content[:120].replace("\n", " ") if isinstance(msg.content, str) else str(msg.content)[:120]
                logger.debug(
                    "[ResolverGraph]   msg[%d] ToolMessage (tool_call_id=%s): '%s...'",
                    i,
                    getattr(msg, "tool_call_id", "?"),
                    preview,
                )
            else:
                logger.debug("[ResolverGraph]   msg[%d] %s", i, msg_type)

        logger.debug("[ResolverGraph]   Sending %d messages to LLM...", len(messages))

        # This is the actual LLM call
        response: AIMessage = llm_with_tools.invoke(messages, config)

        logger.debug(
            "[ResolverGraph]   LLM response received. Type: %s",
            type(response).__name__,
        )

        if hasattr(response, "tool_calls") and response.tool_calls:
            logger.info(
                "[ResolverGraph]   LLM made %d tool call(s) on hop %d:",
                len(response.tool_calls),
                hop,
            )
            for tc in response.tool_calls:
                logger.info(
                    "[ResolverGraph]     → Tool: '%s'  Args: %s",
                    tc.get("name", "?"),
                    json.dumps(tc.get("args", {})),
                )
        else:
            content_preview = (
                response.content[:300].replace("\n", " ")
                if isinstance(response.content, str)
                else str(response.content)[:300]
            )
            logger.info(
                "[ResolverGraph]   LLM produced FINAL response on hop %d (no tool calls):",
                hop,
            )
            logger.debug(
                "[ResolverGraph]   Final response preview: '%s...'", content_preview
            )

        return {
            "messages": [response],
            "hop_count": hop,
        }

    def should_continue(state: ResolverState) -> str:
        """
        Router node — decides what happens after each LLM response.

        Decision logic:
        ───────────────
        1. If hop_count >= max_hops → force END (prevents infinite loops)
        2. If LLM response has tool_calls → route to 'tools' node
        3. Otherwise → LLM gave a final answer → route to END

        This is what makes it a 'loop' — we cycle: resolver → tools → resolver
        until the LLM decides it's done or we hit max_hops.
        """
        last_message = state["messages"][-1]
        hop_count = state.get("hop_count", 0)
        max_hops = state.get("max_hops", MAX_HOPS_DEFAULT)
        chain = state["chain"]

        logger.debug(
            "[ResolverGraph] should_continue: hop=%d/%d, last_msg_type=%s",
            hop_count,
            max_hops,
            type(last_message).__name__,
        )

        # Force-exit if we've exceeded max hops
        if hop_count >= max_hops:
            logger.warning(
                "[ResolverGraph] ⚠️  Max hops (%d) reached for chain at %s:%d — forcing END",
                max_hops,
                state["chain"].file,
                state["chain"].line,
            )
            return "end"

        # If the LLM made tool calls, route to the tools node
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            tool_names = [tc.get("name", "?") for tc in last_message.tool_calls]
            logger.info(
                "[ResolverGraph]   → Route: TOOLS (LLM wants to call: %s)", tool_names
            )
            return "tools"

        # LLM produced a final response — done
        logger.info(
            "[ResolverGraph]   → Route: END (LLM produced final answer after %d hop(s))",
            hop_count,
        )
        return "end"

    # Build the graph
    logger.debug("[ResolverGraph] Defining graph edges...")
    graph = StateGraph(ResolverState)
    graph.add_node("resolver", resolver_node)
    logger.debug("[ResolverGraph]   + node: 'resolver' (LLM reasoning)")
    graph.add_node("tools", tool_node)
    logger.debug("[ResolverGraph]   + node: 'tools' (tool execution)")

    graph.add_edge(START, "resolver")
    logger.debug("[ResolverGraph]   + edge: START → resolver")
    graph.add_conditional_edges(
        "resolver",
        should_continue,
        {"tools": "tools", "end": END},
    )
    logger.debug("[ResolverGraph]   + conditional edges: resolver → tools | END")
    graph.add_edge("tools", "resolver")
    logger.debug("[ResolverGraph]   + edge: tools → resolver (loop back)")

    compiled = graph.compile()
    logger.debug("[ResolverGraph] Graph compiled successfully.")
    return compiled


# ── Result extraction ─────────────────────────────────────────────────────────

def extract_result(state: ResolverState) -> Optional[ApiCall]:
    """
    Parse the LLM's final message into an ApiCall object.

    The LLM is instructed to end with a ```json ... ``` block.
    We extract and parse that block.

    Extraction steps:
    ─────────────────
    1. Find the last AIMessage in the message history
    2. Extract the JSON block from its content (tries 3 regex patterns)
    3. Map fields to ApiCall, coercing None → {} / [] for dict/list fields
    4. If URL is empty, fall back to raw_uri_expr from the chain
    5. If JSON parsing fails, create a fallback UNRESOLVED ApiCall
    """
    chain = state["chain"]
    hop_count = state.get("hop_count", 0)

    logger.debug(
        "[extract_result] ══════════════════════════════════════════"
    )
    logger.debug(
        "[extract_result] Extracting result for: %s (after %d hop(s))",
        chain.summary(),
        hop_count,
    )
    logger.debug(
        "[extract_result] Total messages in state: %d", len(state["messages"])
    )

    # Find last AI message — this is where the LLM's final answer lives
    last_ai = None
    for i, msg in enumerate(reversed(list(state["messages"]))):
        logger.debug(
            "[extract_result]   Scanning message (reverse index %d): type=%s",
            i,
            type(msg).__name__,
        )
        if isinstance(msg, AIMessage):
            last_ai = msg
            logger.debug(
                "[extract_result]   → Found last AIMessage at reverse index %d", i
            )
            break

    if last_ai is None:
        logger.error("[extract_result] ✗ No AI message found in state — this should never happen!")
        return _fallback_api_call(chain, "No AI message in state")

    content = last_ai.content if isinstance(last_ai.content, str) else str(last_ai.content)
    logger.debug(
        "[extract_result] Last AIMessage content (%d chars):\n%s",
        len(content),
        content[:800] + ("..." if len(content) > 800 else ""),
    )

    # Extract JSON block from the LLM response
    logger.debug("[extract_result] Attempting to parse JSON block from LLM response...")
    data = _parse_json_from_response(content)

    if data is None:
        logger.warning(
            "[extract_result] ✗ Could not parse JSON from LLM response for %s",
            chain.summary(),
        )
        logger.warning(
            "[extract_result]   Raw content was: %s", content[:500]
        )
        return _fallback_api_call(chain, f"LLM response did not contain valid JSON: {content[:200]}")

    logger.debug("[extract_result] ✓ JSON parsed successfully. Keys: %s", list(data.keys()))
    logger.debug("[extract_result]   Raw JSON data: %s", json.dumps(data, indent=2)[:1000])

    # Map resolution_status string to enum
    status_str = data.get("resolution_status", "unresolved")
    logger.debug("[extract_result]   resolution_status from LLM: '%s'", status_str)
    try:
        status = ResolutionStatus(status_str)
        logger.debug("[extract_result]   → Mapped to enum: %s", status)
    except ValueError:
        logger.warning(
            "[extract_result]   Unknown resolution_status '%s', defaulting to UNRESOLVED", status_str
        )
        status = ResolutionStatus.UNRESOLVED

    url = data.get("url") or ""
    logger.debug("[extract_result]   url from LLM: '%s'", url)

    # If the LLM returned an empty URL, fall back to the raw expression from
    # the source so the call is never silently dropped by the OpenAPI generator.
    if not url:
        logger.warning(
            "[extract_result]   ⚠️  LLM returned empty URL. Falling back to raw_uri_expr: '%s'",
            chain.raw_uri_expr,
        )
        url = chain.raw_uri_expr or ""
        if status not in (ResolutionStatus.UNRESOLVED,):
            logger.warning(
                "[extract_result]   Overriding status to UNRESOLVED due to empty URL"
            )
            status = ResolutionStatus.UNRESOLVED
    url_template = data.get("url_template") or _extract_path(url)
    logger.debug("[extract_result]   url_template: '%s'", url_template)

    # Coerce None → correct default types for dict/list fields so that Pydantic
    # does not raise a ValidationError when the LLM emits explicit JSON null values.
    def _as_dict(val) -> dict:
        result = val if isinstance(val, dict) else {}
        if val is None:
            logger.debug(
                "[extract_result]   _as_dict: coerced None → {}"
            )
        return result

    def _as_list(val) -> list:
        result = val if isinstance(val, list) else []
        if val is None:
            logger.debug(
                "[extract_result]   _as_list: coerced None → []"
            )
        return result

    logger.debug(
        "[extract_result]   Building ApiCall: method=%s url=%s status=%s hops=%d",
        (data.get("method") or "GET").upper(),
        url,
        status.value,
        hop_count,
    )

    api_call = ApiCall(
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
            "hops": hop_count,
            "class": chain.class_name,
            "host": data.get("host"),
            "scheme": data.get("scheme"),
        },
    )

    logger.info(
        "[extract_result] ✓ ApiCall built: %s %s [%s] (%d hop(s))",
        api_call.method,
        api_call.url,
        api_call.resolution_status.value,
        hop_count,
    )
    if api_call.headers:
        logger.debug("[extract_result]   headers: %s", api_call.headers)
    if api_call.query_params:
        logger.debug("[extract_result]   query_params: %s", api_call.query_params)
    if api_call.path_params:
        logger.debug("[extract_result]   path_params: %s", api_call.path_params)
    if api_call.notes:
        logger.debug("[extract_result]   notes: %s", api_call.notes)

    return api_call


def _parse_json_from_response(content: str) -> Optional[dict]:
    """
    Extract and parse the first JSON block from the LLM response.

    We try three increasingly lenient patterns:
    1. ```json { ... } ```  (ideal — LLM followed instructions)
    2. ``` { ... } ```      (bare code block without language tag)
    3. { ... "method" ... } (raw JSON object somewhere in the text)
    """
    logger.debug("[_parse_json] Trying pattern 1: ```json ... ``` block")
    # Try ```json ... ``` block first
    m = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(1))
            logger.debug("[_parse_json] ✓ Pattern 1 matched and parsed successfully")
            return result
        except json.JSONDecodeError as e:
            logger.debug("[_parse_json]   Pattern 1 JSON decode failed: %s", e)

    logger.debug("[_parse_json] Trying pattern 2: bare ``` ... ``` block")
    # Try bare ```...``` block
    m = re.search(r"```\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(1))
            logger.debug("[_parse_json] ✓ Pattern 2 matched and parsed successfully")
            return result
        except json.JSONDecodeError as e:
            logger.debug("[_parse_json]   Pattern 2 JSON decode failed: %s", e)

    logger.debug("[_parse_json] Trying pattern 3: raw JSON object with 'method' key")
    # Try finding a raw JSON object in the response
    m = re.search(r"(\{[^{}]*\"method\"[^{}]*\})", content, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(1))
            logger.debug("[_parse_json] ✓ Pattern 3 matched and parsed successfully")
            return result
        except json.JSONDecodeError as e:
            logger.debug("[_parse_json]   Pattern 3 JSON decode failed: %s", e)

    logger.debug("[_parse_json] ✗ All 3 patterns failed — no JSON found in LLM response")
    return None


def _extract_path(url: str) -> Optional[str]:
    """Extract just the path portion from a URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path or "/"
        logger.debug("[_extract_path] '%s' → path='%s'", url, path)
        return path
    except Exception as e:
        logger.debug("[_extract_path] urlparse failed for '%s': %s", url, e)
        return url


def _fallback_api_call(chain: RawChain, reason: str) -> ApiCall:
    """
    Create a minimal ApiCall when parsing fails.

    This ensures we NEVER silently drop a detected chain from the output.
    The OpenAPI generator will emit an /unresolved/<expr> path for it.
    """
    logger.warning(
        "[extract_result] Creating FALLBACK ApiCall for %s. Reason: %s",
        chain.summary(),
        reason,
    )
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
    """
    Build the initial LangGraph state for a single chain.

    The state holds:
    ────────────────
    - messages: [SystemMessage, HumanMessage] — the initial conversation
    - chain: the RawChain being resolved (read-only reference)
    - hop_count: 0 (incremented on each resolver→tools cycle)
    - max_hops: hard limit (prevents infinite loops)
    - result: None (populated by extract_result at the end)

    The HumanMessage includes:
    ─────────────────────────
    1. Enclosing Method body FIRST — so the LLM sees local variables
       (e.g. String endpoint = BASE_URL + "/users/") before the chain
    2. The builder chain text
    3. The full class body (truncated at 4000 chars)
    4. The import statements
    """
    logger.debug(
        "[build_initial_state] Building initial LangGraph state for: %s",
        chain.summary(),
    )
    logger.debug("[build_initial_state]   max_hops=%d", max_hops)
    logger.debug(
        "[build_initial_state]   chain has method_context: %s (%d chars)",
        bool(chain.method_context),
        len(chain.method_context),
    )
    logger.debug(
        "[build_initial_state]   chain has class_body: %s (%d chars)",
        bool(chain.class_body),
        len(chain.class_body),
    )
    logger.debug("[build_initial_state]   imports: %d", len(chain.imports))
    logger.debug("[build_initial_state]   raw_uri_expr hint: '%s'", chain.raw_uri_expr)
    logger.debug("[build_initial_state]   suspected_method hint: '%s'", chain.suspected_method)

    method_section = ""
    if chain.method_context:
        method_section = f"""
=== Enclosing Method (READ THIS FIRST to trace local variables) ===
{chain.method_context}
"""
        logger.debug(
            "[build_initial_state]   Injecting method_context section (%d chars). "
            "This lets the LLM see local variable definitions like "
            "'String endpoint = BASE_URL + ...' without needing a tool call.",
            len(chain.method_context),
        )
    else:
        logger.debug(
            "[build_initial_state]   No method_context — chain may be a field-level initializer."
        )

    class_body_preview = chain.class_body[:4000] if len(chain.class_body) > 4000 else chain.class_body
    if len(chain.class_body) > 4000:
        logger.debug(
            "[build_initial_state]   class_body truncated from %d to 4000 chars for LLM context window",
            len(chain.class_body),
        )

    human_content = f"""Analyze this Java HttpClient builder chain and resolve the full API call.

File: {chain.file}
Line: {chain.line}
Class: {chain.class_name}
{method_section}
=== Builder Chain (the HttpRequest built inside the method above) ===
{chain.chain_text}

=== Full Class Context (for field/constant lookups) ===
{class_body_preview}

=== Imports ===
{chr(10).join(chain.imports[:30])}

IMPORTANT: If the URI argument is a local variable (e.g. `endpoint`, `fullUrl`, `sTargetURL`),
look at the Enclosing Method above to find its definition BEFORE calling lookup_symbol().
Local variables are defined in the method body, not in the symbol index.
Only call lookup_symbol() for class-level fields (e.g. BASE_TENANT_URL, API_HOST).
Then respond with the JSON block.
"""

    logger.debug(
        "[build_initial_state] HumanMessage content built (%d chars). "
        "LLM will receive: SystemMessage + HumanMessage = 2 messages total.",
        len(human_content),
    )

    state = ResolverState(
        messages=[
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=human_content),
        ],
        chain=chain,
        hop_count=0,
        max_hops=max_hops,
        result=None,
    )

    logger.debug(
        "[build_initial_state] Initial state ready. "
        "Agentic loop will now start: resolver → (tools → resolver)* → END"
    )
    return state
