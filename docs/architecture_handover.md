# Java REST API Analyzer — Architecture Handover

> **Version:** 2.0 (Agentic Rebuild)  
> **Date:** 2026-07-08  
> **Repository:** [akshaymone/ai-agnets](https://github.com/akshaymone/ai-agnets)  
> **Status:** ✅ Live & tested — 4/4 real-world scenarios passing (Ollama / qwen2.5-coder)

---

## Part 1 — Functional Overview (for Stakeholders)

### What problem does this solve?

Modern Java services make **hundreds of outbound REST API calls** — to payment providers, notification services, internal microservices, etc. These calls are scattered across thousands of lines of code. Keeping API documentation accurate and up-to-date is:

- ❌ **Manual** — engineers must read the code and write docs by hand
- ❌ **Incomplete** — base URLs are often stored in constants in other classes
- ❌ **Stale** — docs drift from reality as code changes

### What does the analyzer do?

![Java REST API Analyzer — How It Works](./functional_overview.jpg)

### Business Value

| Before | After |
|--------|-------|
| Engineers manually read code + write API docs | Tool auto-generates docs by reading code |
| Constants in other classes → URL unknown | AI resolves cross-file constants automatically |
| Docs go stale with every code change | Re-run the tool to refresh docs instantly |
| OpenAPI spec requires expert knowledge | Any Java project → spec in one command |

### Key Capabilities

- 🔍 **Reads Java code** — understands HttpClient builder patterns at the AST level
- 🧠 **Resolves constants** — finds `static final String BASE_URL = "..."` even if it's in a different class
- 📋 **Reads config files** — resolves `@Value("${api.base-url}")` from `.properties`/`.yml`
- 🔄 **Iterative reasoning** — AI asks for more context if it can't resolve something on first pass
- 📄 **OpenAPI 3.1.0 output** — compatible with Swagger UI, Postman, API Gateways

---

## Part 2 — Technical Architecture (for Engineers)

### System Overview

```mermaid
flowchart TD
    subgraph INPUT["📂 Input Layer"]
        JF["Java Source Files\n(.java)"]
        CF["Config Files\n(.properties / .yml)"]
    end

    subgraph SCAN["🔬 Scan Layer  (one-time, before LLM)"]
        FS["FileScanner\nWalks project directory\ncollects all file paths"]
        SIB["SymbolIndexBuilder\ntree-sitter AST scan\nof ALL Java files"]
        SI["SymbolIndex\nIn-memory dict:\n• symbol → value\n• class → file path\n• property key → value"]
        CD["ChainDetector\ntree-sitter detects\nHttpClient builder chains\n→ RawChain objects"]
    end

    subgraph AGENT["🤖 Agent Layer  (LangGraph loop per chain)"]
        RA["ResolverAgent\nOrchestrates one\nchain resolution run"]
        LG["LangGraph ResolverGraph\n━━━━━━━━━━━━━━━━━━━━\nresolver_node  ←→  tools_node\n  LLM reasons      executes tools\n  calls tools       returns results\n━━━━━━━━━━━━━━━━━━━━\nmax hops: 6  |  exits on final JSON"]
    end

    subgraph TOOLS["🛠 Tools  (callable by LLM)"]
        T1["lookup_symbol\nname + context_file\n→ value, type, file, line"]
        T2["get_class_source\nclass_name\n→ full Java source"]
        T3["lookup_property\nproperty key\n→ value from config"]
    end

    subgraph LLM["🧠 LLM Layer"]
        OLLAMA["Ollama\nqwen2.5-coder\n(default / local)"]
        OAI["OpenAI\ngpt-4o etc."]
        ANT["Anthropic\nClaude etc."]
        GGL["Google\nGemini etc."]
    end

    subgraph OUTPUT["📄 Output Layer"]
        AC["ApiCall objects\nmethod, url, headers\nparams, body, cookies\nresolution_status"]
        OAG["OpenAPIGenerator\nmerges + deduplicates\nmultiple calls to same endpoint"]
        SPEC["OpenAPI 3.1.0 JSON\nReady for Swagger UI\nPostman / API Gateways"]
    end

    JF --> FS
    CF --> FS
    FS -->|java_files| SIB
    FS -->|config_files| SIB
    SIB --> SI
    FS -->|java_files| CD
    CD -->|RawChain list| RA

    RA --> LG
    LG <-->|tool calls| TOOLS
    T1 & T2 & T3 -->|queries| SI
    LG -->|resolver_node| OLLAMA & OAI & ANT & GGL

    LG -->|final JSON| RA
    RA -->|ApiCall| AC
    AC --> OAG
    OAG --> SPEC

    style INPUT fill:#1e293b,stroke:#475569,color:#f1f5f9
    style SCAN fill:#1e3a5f,stroke:#3b82f6,color:#f1f5f9
    style AGENT fill:#3b0764,stroke:#a855f7,color:#f1f5f9
    style TOOLS fill:#14532d,stroke:#22c55e,color:#f1f5f9
    style LLM fill:#431407,stroke:#f97316,color:#f1f5f9
    style OUTPUT fill:#1a2e05,stroke:#84cc16,color:#f1f5f9
```

---

### The Agentic Loop — Detail

This is the core of the system. For every detected API call, the LLM runs in a loop:

```mermaid
sequenceDiagram
    participant Code as Java Code
    participant CD as ChainDetector
    participant RA as ResolverAgent
    participant LG as LangGraph
    participant LLM as LLM (qwen2.5-coder)
    participant TL as Tools
    participant SI as SymbolIndex

    Code->>CD: HttpRequest.newBuilder()...build()
    CD->>RA: RawChain {chain_text, method_context, class_body, imports}

    RA->>LG: build_initial_state(chain)
    Note over LG: State: messages, hop_count=0

    LG->>LLM: [SystemPrompt + Chain + Class Context]
    LLM-->>LG: tool_call: lookup_symbol("BASE_TENANT_URL")

    LG->>TL: lookup_symbol("BASE_TENANT_URL", "TenantService.java")
    TL->>SI: index.lookup_symbol(...)
    SI-->>TL: {value: "https://api.enterprise.com/v1", is_static: true}
    TL-->>LG: JSON result

    LG->>LLM: [Previous messages + tool result]
    Note over LLM: Now knows: URL = https://api.enterprise.com/v1/system/health
    LLM-->>LG: Final JSON block {method, url, headers, params...}

    LG-->>RA: final_state (hop_count=1)
    RA->>RA: extract_result(state) → ApiCall
    Note over RA: method=GET, url=https://api.enterprise.com/v1/system/health,<br/>headers={Accept: application/json}, status=llm_resolved
```

> **Loop termination conditions:**
> - ✅ LLM emits final JSON block (no more tool calls) → **DONE**
> - ⚠️ Same symbol requested twice → **exit with partial result**
> - ⚠️ `hop_count >= max_hops` (default: 6) → **force exit with partial result**
> - ❌ LLM/network error → **fallback ApiCall with UNRESOLVED status**

---

### Data Model

```mermaid
classDiagram
    class RawChain {
        +str file
        +int line
        +str chain_text
        +str class_name
        +str class_body
        +str method_context
        +list~str~ imports
        +str package
        +str raw_uri_expr
        +str suspected_method
        +summary() str
    }

    class SymbolEntry {
        +str name
        +str value
        +str java_type
        +str file
        +int line
        +str class_name
        +bool is_static
        +bool is_final
        +to_dict() dict
    }

    class SymbolIndex {
        +lookup_symbol(name, context_file) SymbolEntry
        +lookup_property(key) str
        +class_file(class_name) str
        +get_class_source(class_name) str
        +stats() dict
    }

    class ApiCall {
        +str method
        +str url
        +str url_template
        +ResolutionStatus resolution_status
        +list~str~ path_params
        +dict query_params
        +dict headers
        +dict cookies
        +str body
        +str body_content_type
        +str source_file
        +int source_line
        +str library
        +str raw_url_expression
        +list~str~ notes
        +dict llm_metadata
    }

    class ResolutionStatus {
        <<enumeration>>
        LITERAL
        LLM_RESOLVED
        PARTIAL
        UNRESOLVED
    }

    class ResolverState {
        <<LangGraph State>>
        +list messages
        +RawChain chain
        +int hop_count
        +int max_hops
        +dict result
    }

    SymbolIndex "1" *-- "many" SymbolEntry
    ResolverState --> RawChain
    ApiCall --> ResolutionStatus
```

---

### Module Structure

```mermaid
graph LR
    subgraph pkg["📦 ai_agents package"]
        direction TB

        subgraph scanner["scanner/"]
            FS2["file_scanner.py\nFileScanner"]
            CD2["chain_detector.py\nChainDetector\nRawChain"]
        end

        subgraph index["index/"]
            SI2["symbol_index.py\nSymbolIndex\nSymbolIndexBuilder\nSymbolEntry"]
        end

        subgraph agents["agents/"]
            subgraph tools["tools/"]
                RT["resolver_tools.py\n@tool lookup_symbol\n@tool get_class_source\n@tool lookup_property"]
            end
            GR["graph.py\nResolverState\nbuild_resolver_graph\nextract_result\nbuild_initial_state"]
            RA2["resolver_agent.py\nResolverAgent\n.resolve(chain)\n.resolve_all(chains)\n.resolve_all_async(chains)"]
        end

        subgraph parsers["parsers/"]
            JP["java_parser.py\nJavaParser\n(tree-sitter wrapper)"]
        end

        subgraph llm["llm/"]
            LC["client.py\nget_llm(provider, model)\nOllama | OpenAI\nAnthropic | Google"]
        end

        subgraph models["models/"]
            AC2["api_call.py\nApiCall\nResolutionStatus"]
        end

        subgraph output["output/"]
            OAG2["openapi.py\nOpenAPIGenerator\n.generate(api_calls)\n.to_json(spec)"]
        end

        MAIN["main.py\nCLI: analyze-apis\n5-step pipeline"]
    end

    MAIN --> scanner & index & agents & llm & output
    agents --> index & models & parsers
    scanner --> parsers
    index --> parsers
    output --> models
```

---

### Pipeline — Step by Step

| Step | Module | Input | Output | Time |
|------|--------|-------|--------|------|
| 1 | `FileScanner` | Project root path | `.java` file list, config file list | ~instant |
| 2 | `SymbolIndexBuilder` | All `.java` + config files | `SymbolIndex` (in-memory) | ~1-5s for large codebases |
| 3 | `ChainDetector` | All `.java` files | `List[RawChain]` | ~1-3s |
| 4 | `ResolverAgent` (LangGraph) | Each `RawChain` + `SymbolIndex` | `ApiCall` per chain | ~2-10s per chain (LLM) |
| 5 | `OpenAPIGenerator` | `List[ApiCall]` | OpenAPI 3.1.0 JSON | ~instant |

> **Performance note:** Steps 1–3 are pure Python/tree-sitter (no LLM). Step 4 is the only LLM-dependent step. With `--async`, all chains are resolved in parallel.

---

### LLM Provider Strategy

```mermaid
flowchart LR
    CLI["--llm-provider\n--llm-model"]

    CLI --> F["get_llm(provider, model)"]

    F --> OL["ChatOllama\nLocal, free\nqwen2.5-coder (default)"]
    F --> OAI2["ChatOpenAI\nAPI key needed\ngpt-4o / gpt-4-turbo"]
    F --> ANT2["ChatAnthropic\nAPI key needed\nclaude-3-5-sonnet"]
    F --> GGL2["ChatGoogleGenerativeAI\nAPI key needed\ngemini-2.0-flash"]

    OL & OAI2 & ANT2 & GGL2 --> BT["llm.bind_tools(tools)\nLangChain tool-calling\nworks identically\nacross all providers"]
```

> All providers produce identical output — the `ResolverAgent` and `LangGraph` code is 100% provider-agnostic. Switch providers with one CLI flag, no code changes.

---

### CLI Reference

```bash
# Basic (Ollama must be running locally)
analyze-apis /path/to/java/project

# Save spec to file
analyze-apis /path/to/project -o openapi.json

# Use OpenAI instead of Ollama
analyze-apis /path/to/project \
  --llm-provider openai \
  --llm-model gpt-4o

# More hops for complex cross-file resolution
analyze-apis /path/to/project --max-hops 8

# Parallel (faster for large codebases)
analyze-apis /path/to/project --async

# Full options
analyze-apis /path/to/project \
  --llm-provider ollama \
  --llm-model qwen2.5-coder \
  --max-hops 6 \
  --temperature 0.0 \
  --title "My Service APIs" \
  --api-version 2.1.0 \
  --async \
  -v \
  -o openapi.json
```

---

### Resolution Status Values

| Status | Meaning | Example |
|--------|---------|---------|
| `literal` | URL was a hardcoded string | `URI.create("https://api.example.com/users")` |
| `llm_resolved` | LLM used tools to resolve constants/properties | `BASE_URL + "/health"` → `https://api.enterprise.com/v1/health` |
| `partial` | LLM resolved some fields but not all | URL known, one header value unknown |
| `unresolved` | Could not determine URL (LLM error, network, or truly unknowable) | Method parameter with no traceable value |

---

### What the LLM Sees (Prompt Design)

The LLM receives, **in this order**:
1. **System prompt** — strict instructions: always call `lookup_symbol()` for any URL variable before answering; output a specific JSON schema; never emit `null` for array/dict fields
2. **Enclosing Method body** — the full Java method that contains the builder chain *(new in v2.1)*. This is the most critical context piece: it shows local variable assignments like `String endpoint = BASE_URL + "/users/" + userId + ...` that would otherwise be invisible to the LLM
3. **Builder chain code** — the exact source of the `HttpRequest.newBuilder()...build()` call
4. **Full class context** — up to 4,000 chars of the enclosing class (so it sees field declarations like `static final String BASE_URL = ...`)
5. **Import statements** — to understand cross-class references
6. **Tool results** — injected as `ToolMessage` nodes in the conversation as the loop runs

> **Why Enclosing Method first?** Local variables (e.g. `endpoint`, `fullUrl`, `resourcePath`) are defined in the method body, not in the `SymbolIndex`. If the LLM only sees `URI.create(endpoint)` without seeing `String endpoint = ...`, it guesses wrong. By placing the full method first, the LLM has the complete picture before it even looks at anything else.

The LLM must respond with a strict JSON block:
```json
{
  "method": "GET",
  "url": "https://api.enterprise.com/v1/users/{userId}/documents",
  "url_template": "/v1/users/{userId}/documents",
  "host": "api.enterprise.com",
  "scheme": "https",
  "path_params": ["userId"],
  "query_params": {"filter": "{status}"},
  "headers": { "Authorization": "Bearer token-123" },
  "cookies": {},
  "body": null,
  "body_content_type": null,
  "resolution_status": "llm_resolved",
  "notes": ["userId and status are method parameters"]
}
```

> **Important:** Every array/object field must be present and non-null. The system defensively coerces any `null` the LLM emits to `[]` / `{}` before Pydantic validation.

---

### Robustness & Edge Cases

The pipeline is designed to **never crash and never silently drop a detected call**:

| Failure scenario | What happens |
|------------------|--------------|
| LLM emits `"query_params": null` | `_as_dict()` coercion in `extract_result()` converts to `{}` — Pydantic never sees `None` |
| LLM emits `"url": ""` (blank) | Falls back to `chain.raw_uri_expr` — the call is never lost |
| URL still empty after fallback | OpenAPI generator places call at `/unresolved/<raw_expr_slug>` with a `WARNING` log |
| LLM error / network timeout | `ResolverAgent` catches exception, returns `ApiCall(resolution_status=UNRESOLVED)` with error in `notes` |
| Max hops reached | Graph exits, `extract_result()` parses whatever the last AI message contains |
| LLM resolves local variable to wrong method's URL | Fixed by `method_context` — enclosing method body is in the prompt, so the LLM always traces the right variable |

---

### Test Fixtures

| File | Purpose | Scenarios Covered |
|------|---------|-------------------|
| `tests/java_samples/ApiService.java` | Original 10-pattern test file | Literal URLs, query params, cookies, multi-headers, `.method()`, `newBuilder(URI)` |
| `tests/java_samples/TenantService.java` | Real-world cross-class scenario | Cross-class constants, same-class constants, dynamic path params, header with constant key |
| `tests/java_samples/ApiConstants.java` | Companion constants class | `BASE_TENANT_URL`, `INTERNAL_BASE_URL`, `API_KEY_HEADER` |

---

### Glossary

| Term | Definition |
|------|-----------|
| **tree-sitter** | A parser generator that builds an AST (Abstract Syntax Tree) from source code. Handles any code style, indentation, multiline expressions. |
| **AST** | Abstract Syntax Tree — a structured representation of code that the analyzer traverses to find patterns |
| **HttpClient builder chain** | Java 11+ fluent API: `HttpRequest.newBuilder().uri(...).header(...).GET().build()` |
| **RawChain** | Our data structure representing a single detected builder chain, before any LLM resolution |
| **SymbolIndex** | In-memory dictionary of all `static final` field values, class→file mappings, and config properties, built before the LLM loop starts |
| **LangGraph** | Python library for building stateful, cyclic agent graphs. Manages the `resolver_node ↔ tools_node` loop |
| **ReAct** | Reasoning + Acting — the AI paradigm where the LLM alternates between reasoning (what do I need?) and acting (call a tool) |
| **Hop** | One round-trip through the resolver loop: LLM calls a tool → tool returns result → LLM sees result |
| **OpenAPI 3.1.0** | Industry-standard API specification format. Compatible with Swagger UI, Postman, AWS API Gateway, etc. |
| **Tool-calling** | LLM feature where the model can request execution of a defined function (our `lookup_symbol` etc.) |

---

> [!NOTE]
> **For new engineers joining the project:** Start with [`chain_detector.py`](https://github.com/akshaymone/ai-agnets/blob/main/src/ai_agents/scanner/chain_detector.py) to understand how chains are detected, then [`graph.py`](https://github.com/akshaymone/ai-agnets/blob/main/src/ai_agents/agents/graph.py) to understand the agentic loop.

> [!IMPORTANT]
> **To add a new Java HTTP library** (e.g. RestTemplate, OkHttp): create a new `ChainDetector`-equivalent that detects that library's patterns and emits `RawChain` objects. The rest of the pipeline (SymbolIndex, ResolverAgent, OpenAPIGenerator) works unchanged.

> [!TIP]
> **To swap from Ollama to OpenAI:** just change `--llm-provider openai --llm-model gpt-4o`. GPT-4o and Claude are significantly better at multi-step tool-calling and will typically resolve in fewer hops.
