import argparse
import os
import re
import sys
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse
from pydantic import BaseModel, Field

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser


# ==========================================
# 3. Pydantic Schema (OpenAPI 3.1.0)
# ==========================================

class OpenAPIParameter(BaseModel):
    name: str = Field(description="The name of the parameter.")
    in_: str = Field(alias="in", description="The location of the parameter: 'query', 'header', or 'path'.")
    required: bool = Field(default=True, description="Whether this parameter is mandatory.")
    schema_: Dict[str, Any] = Field(alias="schema", default_factory=lambda: {"type": "string"}, description="The schema defining the type of the parameter.")
    description: Optional[str] = Field(default=None, description="A description of the parameter.")

    model_config = {
        "populate_by_name": True
    }


class OpenAPIRequestBody(BaseModel):
    description: Optional[str] = Field(default=None, description="A description of the request body.")
    required: Optional[bool] = Field(default=None, description="Whether the request body is required.")
    content: Dict[str, Dict[str, Any]] = Field(
        description="A map of media types (e.g., 'application/json') to their schema objects: {'application/json': {'schema': {...}}}"
    )


class OpenAPIEndpoint(BaseModel):
    summary: str = Field(description="A short summary of what the endpoint does.")
    method: str = Field(description="The HTTP method (e.g., GET, POST, PUT, DELETE, PATCH).")
    path: str = Field(description="The path portion of the URL (e.g., '/api/v1/users' or '/api/v1/users/{userId}').")
    parameters: List[OpenAPIParameter] = Field(default_factory=list, description="List of parameters (query, header, path).")
    requestBody: Optional[OpenAPIRequestBody] = Field(default=None, description="The request body details if applicable.")


# ==========================================
# Mock LLM classes for environment fallback
# ==========================================

class MockStructuredLLM:
    def __init__(self, schema_class):
        self.schema_class = schema_class

    def invoke(self, prompt: str) -> OpenAPIEndpoint:
        summary = "Java HttpClient Request"
        method = "GET"
        path = "/"
        parameters = []
        request_body = None

        # Extract only the Java source code block from the prompt to avoid matching prompt instructions
        code_block_match = re.search(r'```java\s*(.*?)\s*```', prompt, re.DOTALL)
        src_context = code_block_match.group(1) if code_block_match else prompt

        # 1. Extract URL/Path
        url_match = re.search(r'"(https?://[^"]+)"', src_context)
        if url_match:
            full_url = url_match.group(1)
            try:
                parsed_url = urlparse(full_url)
                path = parsed_url.path
            except Exception:
                path = "/"
        else:
            path_match = re.search(r'"(/[^"]+)"', src_context)
            if path_match:
                path = path_match.group(1)

        # 2. Extract Method
        if ".GET()" in src_context:
            method = "GET"
        elif ".POST(" in src_context:
            method = "POST"
        elif ".PUT(" in src_context:
            method = "PUT"
        elif ".DELETE()" in src_context:
            method = "DELETE"
        else:
            # Fallback based on lowercase words in enclosing method code
            lower_src = src_context.lower()
            if "post" in lower_src:
                method = "POST"
            elif "put" in lower_src:
                method = "PUT"
            elif "delete" in lower_src:
                method = "DELETE"
            elif "get" in lower_src:
                method = "GET"

        # 3. Extract Headers
        header_matches = re.findall(r'\.header\s*\(\s*"([^"]+)"\s*,\s*([^)]+)\)', src_context)
        for header_name, header_val in header_matches:
            parameters.append(
                OpenAPIParameter(
                    name=header_name,
                    in_="header",
                    required=True,
                    schema={"type": "string"},
                    description=f"Header parameter {header_name}"
                )
            )

        # 4. Extract Query Parameters from Path
        if "?" in path:
            path_part, query_part = path.split("?", 1)
            path = path_part
            for item in query_part.split("&"):
                if "=" in item:
                    k, _ = item.split("=", 1)
                    parameters.append(
                        OpenAPIParameter(
                            name=k,
                            in_="query",
                            required=False,
                            schema={"type": "string"},
                            description=f"Query parameter {k}"
                        )
                    )

        # 5. Extract Request Body (if POST/PUT)
        if method in ("POST", "PUT"):
            body_match = re.search(r'bodyJson\s*=\s*"((?:[^"\\]|\\.)*)"', src_context)
            schema = {"type": "object"}
            if body_match:
                import json
                try:
                    body_str = body_match.group(1).replace('\\"', '"')
                    body_data = json.loads(body_str)
                    properties = {}
                    for k, v in body_data.items():
                        t = "string"
                        if isinstance(v, bool):
                            t = "boolean"
                        elif isinstance(v, (int, float)):
                            t = "number"
                        properties[k] = {"type": t}
                    schema = {"type": "object", "properties": properties}
                except Exception:
                    pass

            request_body = OpenAPIRequestBody(
                description="Request body extracted from Java source code",
                required=True,
                content={
                    "application/json": {
                        "schema": schema
                    }
                }
            )

        # 6. Extract Path Parameters
        segments = path.split("/")
        new_segments = []
        for segment in segments:
            if segment.isdigit():
                param_name = "id"
                while any(p.name == param_name for p in parameters):
                    param_name += "_"
                parameters.append(
                    OpenAPIParameter(
                        name=param_name,
                        in_="path",
                        required=True,
                        schema={"type": "integer"},
                        description=f"Path parameter {param_name}"
                    )
                )
                new_segments.append(f"{{{param_name}}}")
            else:
                new_segments.append(segment)
        path = "/".join(new_segments)

        return self.schema_class(
            summary=f"Extracted {method} request for {path}",
            method=method,
            path=path,
            parameters=parameters,
            requestBody=request_body
        )


class MockLLM:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def with_structured_output(self, schema_class):
        return MockStructuredLLM(schema_class)


# ==========================================
# 4. LLM Factory
# ==========================================

def get_llm():
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    is_mock = os.environ.get("MOCK_LLM", "false").lower() == "true" or not api_key or api_key.lower().startswith("mock")

    if provider == "ollama":
        if is_mock:
            return MockLLM("llama3")
        from langchain_ollama import ChatOllama
        return ChatOllama(model="llama3", temperature=0)
    else:  # gemini
        if is_mock:
            return MockLLM("gemini-2.5-flash")
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model="gemini-2.5-flash", api_key=api_key, temperature=0)


# ==========================================
# 2. Tree-sitter Extraction
# ==========================================

def get_identifiers(node) -> set:
    identifiers = set()
    if node.type == "identifier":
        identifiers.add(node.text.decode("utf-8"))
    for child in node.children:
        identifiers.update(get_identifiers(child))
    return identifiers


def extract_http_calls_from_java(filepath: str) -> List[Dict[str, Any]]:
    parser = Parser(Language(tsjava.language()))
    with open(filepath, "rb") as f:
        code_bytes = f.read()
    tree = parser.parse(code_bytes)

    invocations = []

    def walk(node):
        if node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node:
                method_name = name_node.text.decode("utf-8")
                if method_name in ("send", "execute"):
                    invocations.append(node)
        for child in node.children:
            walk(child)

    walk(tree.root_node)

    results = []
    for invocation in invocations:
        # Find enclosing method declaration
        curr = invocation.parent
        method_decl = None
        while curr:
            if curr.type == "method_declaration":
                method_decl = curr
                break
            curr = curr.parent

        if not method_decl:
            continue

        # Collect preceding variable declarations/assignments in the method block
        preceding = []

        def collect_preceding(n):
            if n.end_byte <= invocation.start_byte:
                if n.type == "local_variable_declaration":
                    preceding.append(n)
                    return
                elif n.type == "assignment_expression":
                    preceding.append(n)
                    return
            for child in n.children:
                collect_preceding(child)

        collect_preceding(method_decl)

        # Dependency trace
        arg_node = invocation.child_by_field_name("arguments")
        target_vars = get_identifiers(arg_node) if arg_node else set()

        relevant_nodes = set()
        changed = True
        while changed:
            changed = False
            for node in sorted(preceding, key=lambda n: n.start_byte, reverse=True):
                if node in relevant_nodes:
                    continue
                is_relevant = False
                new_vars = set()

                if node.type == "local_variable_declaration":
                    for child in node.children:
                        if child.type == "variable_declarator":
                            name_node = child.child_by_field_name("name")
                            if name_node:
                                name_str = name_node.text.decode("utf-8")
                                if name_str in target_vars:
                                    is_relevant = True
                                    val_node = child.child_by_field_name("value")
                                    if val_node:
                                        new_vars.update(get_identifiers(val_node))
                elif node.type == "assignment_expression":
                    left_node = node.child_by_field_name("left")
                    if left_node:
                        left_vars = get_identifiers(left_node)
                        if left_vars.intersection(target_vars):
                            is_relevant = True
                            right_node = node.child_by_field_name("right")
                            if right_node:
                                new_vars.update(get_identifiers(right_node))

                if is_relevant:
                    relevant_nodes.add(node)
                    target_vars.update(new_vars)
                    changed = True

        results.append({
            "enclosing_method": method_decl.text.decode("utf-8"),
            "http_call": invocation.text.decode("utf-8"),
            "traced_assignments": [n.text.decode("utf-8") for n in sorted(relevant_nodes, key=lambda n: n.start_byte)]
        })

    return results


# ==========================================
# 5. AI Execution
# ==========================================

def run_ai_extraction(context: Dict[str, Any]) -> str:
    llm = get_llm()
    structured_llm = llm.with_structured_output(OpenAPIEndpoint)

    prompt = f"""You are an expert utility designed to analyze Java HTTP client source code and generate a valid OpenAPI 3.1.0 schema for the target HTTP endpoint.

Here is the context extracted from the Java AST using Tree-sitter:

Enclosing Method Source Code:
```java
{context['enclosing_method']}
```

Target HTTP client invocation line:
```java
{context['http_call']}
```

Traced Variable Assignments/Declarations:
{chr(10).join(f"- {line}" for line in context['traced_assignments'])}

Based on the above Java code, identify:
1. The HTTP method used (e.g. GET, POST, PUT, DELETE).
2. The URL / path (resolve variables like 'sTargetURL' or 'urlStr'). If the path contains dynamic IDs (e.g. '/users/12345'), represent it as a path parameter (e.g. '/users/{{id}}') and define it in parameters.
3. Query parameters, Header parameters (like 'Authorization' or 'Content-Type'), or Path parameters.
4. The Request Body schema if a POST/PUT body is present (e.g., if a JSON payload is passed).

Please populate the OpenAPIEndpoint schema fields exactly as defined in the target model.
"""
    result = structured_llm.invoke(prompt)
    return result.model_dump_json(indent=2, by_alias=True)


# ==========================================
# Helper to Find Java Files
# ==========================================

def find_java_files(directory: str) -> list[str]:
    """Recursively walks the directory and finds all files ending with .java."""
    java_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.java'):
                java_files.append(os.path.join(root, file))
    return java_files


# ==========================================
# CLI Entry Point
# ==========================================

def main():
    parser = argparse.ArgumentParser(description="Analyze Java source files to map out REST API calls.")
    parser.add_argument("directory", nargs="?", default=None, help="Path to the directory containing Java files")

    args = parser.parse_args()

    if not args.directory:
        print("Error: No directory path provided. Please specify a directory path.", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    directory = args.directory
    if not os.path.exists(directory):
        print(f"Error: The directory '{directory}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isdir(directory):
        print(f"Error: '{directory}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    java_files = find_java_files(directory)

    print(f"Total Java files found: {len(java_files)}")
    for filepath in sorted(java_files):
        print(f"\nAnalyzing file: {filepath}")
        try:
            calls = extract_http_calls_from_java(filepath)
            print(f"Found {len(calls)} HTTP calls.")
            for i, call in enumerate(calls):
                print(f"\n--- HTTP Call #{i + 1} ---")
                print(f"Invocation: {call['http_call']}")
                openapi_json = run_ai_extraction(call)
                print("Generated OpenAPI Specification:")
                print(openapi_json)
        except Exception as e:
            print(f"Error analyzing {filepath}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
