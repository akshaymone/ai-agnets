"""
Analyzer for java.net.http.HttpClient (Java 11+) REST API calls.

Detection strategy
──────────────────
java.net.http.HttpClient uses a fluent builder pattern:

    HttpRequest request = HttpRequest.newBuilder()
        .uri(URI.create("https://api.example.com/users/{id}"))
        .header("Content-Type", "application/json")
        .header("Cookie", "session=abc; user=joe")
        .POST(HttpRequest.BodyPublishers.ofString(body))
        .build();

In the AST this becomes a deeply-nested chain of `method_invocation`
nodes, each wrapping the previous one as its `object` child:

    method_invocation(build)
      └─ object: method_invocation(POST)
           └─ object: method_invocation(header)
                └─ object: method_invocation(header)
                     └─ object: method_invocation(uri)
                          └─ object: method_invocation(newBuilder)
                               └─ object: field_access(HttpRequest.newBuilder)

Algorithm
─────────
1. Traverse the AST for every `method_invocation` whose name == "build"
   and whose chain contains "newBuilder" referencing HttpRequest.
2. Unwrap the chain bottom-up into an ordered list of (method, args).
3. Dispatch each call in the list to the appropriate extractor.
4. Optionally pass dynamic/unresolved URIs to an LLM for normalization.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from tree_sitter import Node

from ...models.api_call import ApiCall, ResolutionStatus
from ...parsers.java_parser import JavaParser
from ..base import BaseAnalyzer

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_LIBRARY_NAME = "java.net.http.HttpClient"

# Signals that the file likely uses this library
_FINGERPRINTS = (b"HttpRequest", b"HttpClient", b"java.net.http")

# HTTP methods that carry no body
_BODYLESS_METHODS = {"GET", "DELETE", "HEAD", "OPTIONS"}

# BodyPublisher factory methods → body type mapping
_PUBLISHER_CONTENT_TYPES: Dict[str, str] = {
    "ofString": "text/plain",            # refined to application/json later if header says so
    "ofInputStream": "application/octet-stream",
    "ofFile": "application/octet-stream",
    "ofByteArray": "application/octet-stream",
    "ofByteArrays": "application/octet-stream",
    "noBody": "",
}


# ── Helper: chain unwrapping ──────────────────────────────────────────────────

def _unwrap_chain(node: Node, source: bytes) -> List[Dict[str, Any]]:
    """
    Walk a nested method_invocation chain and collect each call.

    Returns a list of dicts (oldest first):
        [{"name": str, "args_text": [str], "args_nodes": [Node]}, …]
    """
    calls: List[Dict[str, Any]] = []
    _collect(node, source, calls)
    calls.reverse()  # innermost (oldest) first
    return calls


def _collect(node: Node, source: bytes, calls: List[Dict[str, Any]]) -> None:
    """Recursive helper for _unwrap_chain."""
    if node.type not in ("method_invocation", "field_access"):
        return

    if node.type == "method_invocation":
        call: Dict[str, Any] = {"name": None, "args_text": [], "args_nodes": [], "node": node}
        child_object: Optional[Node] = None

        for child in node.children:
            if child.type == "identifier":
                call["name"] = source[child.start_byte : child.end_byte].decode("utf-8")
            elif child.type == "argument_list":
                call["args_nodes"], call["args_text"] = _extract_args(child, source)
            elif child.type in ("method_invocation", "field_access"):
                child_object = child

        if call["name"]:
            calls.append(call)
        if child_object is not None:
            _collect(child_object, source, calls)

    elif node.type == "field_access":
        # e.g. HttpRequest.newBuilder — recurse into the object part
        for child in node.children:
            if child.type in ("method_invocation", "field_access", "identifier"):
                _collect(child, source, calls)


def _extract_args(
    arg_list_node: Node, source: bytes
) -> Tuple[List[Node], List[str]]:
    """Extract argument nodes and their text from an argument_list node."""
    nodes: List[Node] = []
    texts: List[str] = []
    for child in arg_list_node.children:
        if child.type not in ("(", ")", ","):
            nodes.append(child)
            texts.append(source[child.start_byte : child.end_byte].decode("utf-8", errors="replace").strip())
    return nodes, texts


# ── Helper: URI extraction ────────────────────────────────────────────────────

def _extract_uri_string(arg_text: str) -> Tuple[str, ResolutionStatus]:
    """
    Pull the URL string out of a URI argument expression.

    Handles:
      - URI.create("https://…")
      - new URI("https://…")
      - "https://…"  (bare string)
      - Everything else → DYNAMIC / UNRESOLVED

    Returns (url, resolution_status).
    """
    # URI.create("...") or new URI("...")
    match = re.search(r'(?:URI\.create|new\s+URI)\s*\(\s*"([^"]+)"', arg_text)
    if match:
        return match.group(1), ResolutionStatus.LITERAL

    # Bare string literal
    match = re.search(r'^"([^"]+)"$', arg_text.strip())
    if match:
        return match.group(1), ResolutionStatus.LITERAL

    # URI.create(someVariable)  or  URI.create(base + "/path")
    inner_match = re.search(r'(?:URI\.create|new\s+URI)\s*\((.+)\)', arg_text, re.DOTALL)
    if inner_match:
        inner = inner_match.group(1).strip()
        return inner, ResolutionStatus.DYNAMIC

    return arg_text, ResolutionStatus.UNRESOLVED


def _parse_url(url: str) -> Tuple[str, Dict[str, str], List[str]]:
    """
    Split a URL into (path_template, query_params, path_params).

    Handles {template} placeholders already present in the URL.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url, {}, []

    path = parsed.path or "/"

    # Extract query params
    query_params: Dict[str, str] = {}
    if parsed.query:
        qs = parse_qs(parsed.query, keep_blank_values=True)
        query_params = {k: v[0] if v else "" for k, v in qs.items()}

    # Path params as {name} placeholders already in the path
    path_params = re.findall(r"\{([^}]+)\}", path)

    return path, query_params, path_params


def _infer_path_params_from_dynamic(raw_expr: str) -> List[str]:
    """
    Heuristically extract parameter names from a dynamic URI expression
    like  URI.create(baseUrl + "/users/" + userId + "/orders/" + orderId).
    """
    # Find variable names in string concatenation after literal segments
    parts = re.split(r'"[^"]*"', raw_expr)
    params: List[str] = []
    for part in parts:
        # Strip operators and whitespace, look for simple identifiers
        candidates = re.findall(r'\b([a-z][a-zA-Z0-9_]*(?:Id|Name|Key|Code|Type|Uuid)?)\b', part)
        params.extend(c for c in candidates if c not in ("URI", "create", "new", "String", "format"))
    return list(dict.fromkeys(params))  # deduplicate, preserve order


# ── Helper: body extraction ───────────────────────────────────────────────────

def _extract_body_info(args_text: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract (body_content, inferred_content_type) from BodyPublisher args.

    args_text: the arguments to the HTTP method call (POST/PUT/PATCH/method)
    """
    if not args_text:
        return None, None

    publisher_expr = args_text[-1]  # Last arg is always the BodyPublisher

    for factory_method, content_type in _PUBLISHER_CONTENT_TYPES.items():
        if factory_method in publisher_expr:
            # Try to extract the literal passed to the factory
            inner = re.search(rf'{factory_method}\s*\(\s*"([^"]+)"', publisher_expr)
            body_content = inner.group(1) if inner else publisher_expr
            return body_content, content_type or None

    # noBody
    if "noBody" in publisher_expr or publisher_expr.strip() == "":
        return None, None

    return publisher_expr, None  # Unknown publisher


# ── Helper: cookie parsing ────────────────────────────────────────────────────

def _parse_cookie_header(value: str) -> Dict[str, str]:
    """Parse 'name=val; name2=val2' into a dict."""
    cookies: Dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
        elif part:
            cookies[part] = ""
    return cookies


# ── Main analyzer ─────────────────────────────────────────────────────────────

class JavaNetHttpClientAnalyzer(BaseAnalyzer):
    """
    Detects outbound REST API calls made via java.net.http.HttpClient.

    Supports:
      - .GET(), .POST(body), .PUT(body), .DELETE(), .HEAD()
      - .method("PATCH", body)
      - .uri(URI.create("…"))  and  .uri(URI.create(variable))
      - .header("key", "value")
      - .headers("k1","v1","k2","v2",…)
      - Cookie parsing from the Cookie header value
      - Query parameter extraction from literal URIs
      - Heuristic path-param inference for dynamic URIs
    """

    library_name = _LIBRARY_NAME

    def __init__(self, java_parser: JavaParser) -> None:
        self._parser = java_parser

    # ── BaseAnalyzer interface ─────────────────────────────────────────────────

    def can_handle(self, source: bytes) -> bool:
        """Return True if the file probably uses java.net.http.HttpClient."""
        return any(fp in source for fp in _FINGERPRINTS)

    def analyze_file(self, file_path: str) -> List[ApiCall]:
        """Parse *file_path* and return all detected API calls."""
        tree, source = self._parser.parse_file(file_path)
        if not self.can_handle(source):
            return []

        api_calls: List[ApiCall] = []
        self._traverse(tree.root_node, source, file_path, api_calls)
        logger.info(
            "[%s] %s → %d call(s) found",
            self.library_name, file_path, len(api_calls),
        )
        return api_calls

    # ── AST traversal ─────────────────────────────────────────────────────────

    def _traverse(
        self,
        node: Node,
        source: bytes,
        file_path: str,
        results: List[ApiCall],
    ) -> None:
        """
        Walk every node in the AST.
        When we spot a .build() that belongs to an HttpRequest chain, extract it.
        """
        if self._is_http_request_build(node, source):
            call = self._extract_api_call(node, source, file_path)
            if call:
                results.append(call)
            # Don't recurse *into* this chain — we've consumed it
            return

        for child in node.children:
            self._traverse(child, source, file_path, results)

    def _is_http_request_build(self, node: Node, source: bytes) -> bool:
        """
        Return True if *node* is a method_invocation named "build" whose
        enclosing chain originates from HttpRequest.newBuilder().
        """
        if node.type != "method_invocation":
            return False

        # Check this node's method name is "build"
        method_name = None
        for child in node.children:
            if child.type == "identifier":
                method_name = source[child.start_byte : child.end_byte].decode("utf-8")
                break

        if method_name != "build":
            return False

        # Check that the full chain text contains HttpRequest + newBuilder
        full_text = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
        return "newBuilder" in full_text and (
            "HttpRequest" in full_text or "java.net.http" in full_text
        )

    # ── Chain extraction ───────────────────────────────────────────────────────

    def _extract_api_call(
        self, build_node: Node, source: bytes, file_path: str
    ) -> Optional[ApiCall]:
        """
        Unwrap the builder chain and convert it to an ApiCall.
        """
        chain = _unwrap_chain(build_node, source)
        if not chain:
            return None

        # Accumulated state from the chain
        raw_uri_expr: str = ""
        url: str = ""
        url_template: Optional[str] = None
        resolution_status = ResolutionStatus.UNRESOLVED
        http_method: Optional[str] = None
        headers: Dict[str, str] = {}
        cookies: Dict[str, str] = {}
        body: Optional[str] = None
        body_content_type: Optional[str] = None
        query_params: Dict[str, str] = {}
        path_params: List[str] = []
        notes: List[str] = []

        for call in chain:
            name = call["name"]
            args_text: List[str] = call["args_text"]

            # ── URI ──────────────────────────────────────────────────────────
            if name == "uri" and args_text:
                raw_uri_expr = args_text[0]
                url, resolution_status = _extract_uri_string(raw_uri_expr)
                url_template_candidate, query_params, path_params = _parse_url(url)
                url_template = url_template_candidate

                if resolution_status == ResolutionStatus.DYNAMIC:
                    notes.append(
                        f"URI is dynamically constructed: {raw_uri_expr!r} — "
                        "path params inferred heuristically; LLM enhancement recommended."
                    )
                    path_params = _infer_path_params_from_dynamic(raw_uri_expr)
                    # Build a rough template showing where variable parts go
                    url_template = _build_dynamic_template(raw_uri_expr)

            # ── URI passed directly to newBuilder(uri) ────────────────────────
            elif name == "newBuilder" and args_text:
                raw_uri_expr = args_text[0]
                url, resolution_status = _extract_uri_string(raw_uri_expr)
                url_template, query_params, path_params = _parse_url(url)

            # ── HTTP method (bodyless) ────────────────────────────────────────
            elif name in _BODYLESS_METHODS:
                http_method = name.upper()

            # ── HTTP method (with body) ───────────────────────────────────────
            elif name in ("POST", "PUT", "PATCH"):
                http_method = name.upper()
                body, body_content_type = _extract_body_info(args_text)

            # ── .method("VERB", bodyPublisher) ───────────────────────────────
            elif name == "method" and len(args_text) >= 2:
                http_method = args_text[0].strip('"').upper()
                body, body_content_type = _extract_body_info(args_text[1:])

            # ── Single header ─────────────────────────────────────────────────
            elif name == "header" and len(args_text) >= 2:
                key = _strip_quotes(args_text[0])
                val = _strip_quotes(args_text[1])
                if key.lower() == "cookie":
                    cookies.update(_parse_cookie_header(val))
                else:
                    headers[key] = val

            # ── Multi-header: .headers("k","v","k2","v2",...) ─────────────────
            elif name == "headers" and len(args_text) >= 2:
                for i in range(0, len(args_text) - 1, 2):
                    key = _strip_quotes(args_text[i])
                    val = _strip_quotes(args_text[i + 1])
                    if key.lower() == "cookie":
                        cookies.update(_parse_cookie_header(val))
                    else:
                        headers[key] = val

        # ── Post-processing ───────────────────────────────────────────────────

        if not http_method:
            http_method = "GET"  # default for java.net.http.HttpClient
            notes.append("No explicit HTTP method found in builder chain — defaulting to GET.")

        # Refine body content type using Content-Type header (if present)
        ct_header = headers.get("Content-Type") or headers.get("content-type")
        if ct_header and body_content_type == "text/plain":
            body_content_type = ct_header

        if not url:
            logger.debug("Skipping chain at line %d — no URI found", build_node.start_point[0] + 1)
            return None

        return ApiCall(
            method=http_method,
            url=url,
            url_template=url_template,
            resolution_status=resolution_status,
            path_params=path_params,
            query_params=query_params,
            headers=headers,
            cookies=cookies,
            body=body,
            body_content_type=body_content_type,
            source_file=file_path,
            source_line=build_node.start_point[0] + 1,
            library=_LIBRARY_NAME,
            raw_url_expression=raw_uri_expr,
            notes=notes,
        )


# ── Utility helpers ───────────────────────────────────────────────────────────

def _strip_quotes(text: str) -> str:
    """Remove surrounding double-quotes from a string literal."""
    text = text.strip()
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    return text


def _build_dynamic_template(raw_expr: str) -> str:
    """
    Attempt to build a rough URL template from a dynamic URI expression.

    Example:
        URI.create(baseUrl + "/users/" + userId + "/orders/" + orderId)
        →  <baseUrl>/users/{userId}/orders/{orderId}
    """
    # Strip outer URI.create(...) or new URI(...)
    inner = re.sub(r'(?:URI\.create|new\s+URI)\s*\((.+)\)', r'\1', raw_expr, flags=re.DOTALL).strip()

    segments: List[str] = []
    for part in re.split(r'\s*\+\s*', inner):
        part = part.strip()
        if part.startswith('"') and part.endswith('"'):
            # Literal segment — keep as-is, strip quotes
            literal = part[1:-1]
            # Remove protocol+host prefix (we want path only)
            path_part = re.sub(r'^https?://[^/]+', '', literal)
            segments.append(path_part)
        else:
            # Variable — wrap as path param placeholder
            # Strip common prefixes like "String " etc.
            var_name = re.sub(r'^.*\s', '', part)
            segments.append(f"{{{var_name}}}")

    template = "".join(segments)
    # Normalise double slashes
    template = re.sub(r"/{2,}", "/", template)
    return template or raw_expr
