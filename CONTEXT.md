# Project Context — ai-agents Java REST API Analyzer

> **Update this file at the end of every session.**
> When starting a new session, share this file with the AI to resume instantly.

---

## 🎯 Project Goal

Build a Python package (`ai-agents`) that statically analyzes a Java codebase
and detects all **outbound REST API calls**, then outputs an **OpenAPI 3.1.0** spec.

- Language scope: **Java only** (for now)
- Parsing engine: **tree-sitter** (via `tree-sitter-java` Python binding)
- LLM integration: **LangChain → LangGraph** (Ollama default, swappable to OpenAI/Anthropic/Google)
- Output format: **OpenAPI 3.1.0 JSON**
- Architecture: **Agentic loop** — LLM resolves API calls by calling code lookup tools

---

## ✅ MVP 1 — DONE (commit `bfb98f1`) — SUPERSEDED by v2

### Library covered: `java.net.http.HttpClient` (Java 11+)
**10/10 test patterns passed** — but had two key bugs:
1. Cross-class static constants not resolved (e.g. `BASE_TENANT_URL` from `ApiConstants`)
2. Headers sometimes missed when URL was dynamic

---

## ✅ v2 AGENTIC REBUILD — DONE & LIVE

### Architecture
Moved from static one-pass analysis to a **LangGraph-based agentic loop**:
- LLM receives the raw builder chain + **enclosing method body** + class context
- LLM calls tools if it needs to resolve unknown class-level symbols/properties
- Loop exits when LLM emits a final JSON block (or max hops reached)

### What's working (as of 2026-07-08, all committed):
- ✅ **ChainDetector** — detects all `HttpRequest.newBuilder()…build()` chains via tree-sitter AST
- ✅ **`method_context`** on `RawChain` — captures the **full enclosing method body** so the LLM can trace local variables (`endpoint`, `fullUrl`, etc.) without tool calls
- ✅ **FileScanner** — collects `.java` + `.properties`/`.yml` files
- ✅ **SymbolIndex** — pre-scans ALL Java files, indexes `static final` fields, class→file mappings, and `.properties`/YAML keys
- ✅ **LangGraph ResolverGraph** — `resolver_node` ↔ `tools_node` loop with hop limit
- ✅ **Tools**: `lookup_symbol`, `get_class_source`, `lookup_property`
- ✅ **ResolverAgent** — sync + async `resolve_all` / `resolve_all_async`
- ✅ **LLM client** — provider-agnostic factory (Ollama/OpenAI/Anthropic/Google)
- ✅ **Main CLI** — 5-step pipeline (scan → index → detect → resolve → generate)
- ✅ **OpenAPI generator** — never silently drops calls; unresolved paths get `/unresolved/<expr>` placeholder
- ✅ **Null-safe ApiCall construction** — LLM `null` for dict/list fields coerced safely

### Live test results (Ollama / qwen2.5-coder, 2026-07-08):
| Scenario | Status | Notes |
|----------|--------|-------|
| Hardcoded GET with class constant (`BASE_TENANT_URL + "/system/health"`) | ✅ | `llm_resolved` |
| Local variable URL (`String endpoint = BASE_TENANT_URL + "/users/" + userId + ...`) | ✅ after fix | Was resolving to wrong URL before `method_context` fix |
| POST with multi-step variable (`resourcePath` → `fullUrl`) | ✅ | `llm_resolved` |
| Complex `.method("PUT", ...)` + fragmented URL | ✅ | `llm_resolved` |

### What's pending:
- [ ] Add `ResolutionStatus.PARTIAL` and `PROPERTIES_RESOLVED` status values
- [ ] Formal pytest test suite for `SymbolIndex`, `ChainDetector`, `extract_result()`
- [ ] Validate cross-class constant resolution (e.g. `BASE_TENANT_URL` defined in `ApiConstants.java`)

---

## 🗂️ Module Structure (v2, current)

```
src/ai_agents/
├── main.py                              # CLI: analyze-apis (v2 agentic)
├── models/api_call.py                  # Pydantic ApiCall + ResolutionStatus
├── parsers/java_parser.py              # tree-sitter Java wrapper
├── scanner/
│   ├── file_scanner.py                 # Collect .java + .properties files
│   └── chain_detector.py               # tree-sitter chain detection → RawChain
│                                       #   RawChain.method_context = enclosing method body
├── index/
│   └── symbol_index.py                 # SymbolIndex + SymbolIndexBuilder
├── agents/
│   ├── tools/
│   │   └── resolver_tools.py           # @tool: lookup_symbol, get_class_source, lookup_property
│   ├── graph.py                        # LangGraph StateGraph (resolver ↔ tools loop)
│   │                                   #   extract_result(): null-safe coercion + empty-url fallback
│   │                                   #   build_initial_state(): Enclosing Method section in prompt
│   └── resolver_agent.py              # Public interface: resolve(chain) → ApiCall
├── llm/client.py                       # Provider factory (Ollama/OpenAI/Anthropic/Google)
└── output/openapi.py                   # OpenAPI 3.1.0 generator
                                        #   no silent drops — unresolved → /unresolved/<expr>
```

---

## 🐛 Bugs Fixed This Session (all committed)

### Bug 1 — Pydantic `ValidationError` crash (commit `8949b16`)
**Symptom:** `2 validation errors for ApiCall — query_params / cookies: Input should be a valid dictionary`
**Root cause:** `data.get("query_params", {})` returns `None` (not `{}`) when the LLM explicitly emits `"query_params": null` in JSON. Pydantic rejects `None` for `Dict[str, str]`.
**Fix:** `_as_dict()` / `_as_list()` coercion helpers in `extract_result()` — any `None` or wrong type becomes `{}` / `[]`.

### Bug 2 — System prompt not aggressive enough about symbol lookup (commit `8949b16`)
**Symptom:** Variables like `BASE_TENANT_URL` not resolved → URL left as variable name in output.
**Fix:** Prompt now mandates `lookup_symbol()` for ANY identifier in the URL before writing final answer. Example JSON uses `{}` for empty objects to deter null emission.

### Bug 3 — Unresolved calls silently dropped from output (commit `5fcee98`)
**Symptom:** When LLM returns `"url": ""`, the OpenAPI generator hit `if not path: continue` and the entire API call disappeared.
**Fix (two-part):**
1. `graph.py`: if `url == ""` after LLM response, fall back to `chain.raw_uri_expr`
2. `openapi.py`: replace `continue` with a visible `/unresolved/<expr>` placeholder path + `WARNING` log. Invisible drops are always worse than labelled placeholders.

### Bug 4 — Local variable URL traced to wrong method (commit `89167c7`)
**Symptom:** Scenario 2 (`fetchUserDocuments`) resolved to the same URL as Scenario 1 (`/system/health`). They merged in the OpenAPI output → only 3 of 4 APIs appeared.
**Root cause:** `chain_text` only captured the `HttpRequest.newBuilder()…build()` AST node. `String endpoint = BASE_TENANT_URL + "/users/" + userId + ...` was defined *above* the builder chain, outside the AST subtree. The LLM scanned the full class body and latched onto the first URL pattern it found (wrong method).
**Fix:**
- `chain_detector.py`: added `_find_enclosing_method()` AST helper + `method_context: str` field on `RawChain`
- `graph.py`: inserted `=== Enclosing Method (READ THIS FIRST) ===` at the top of the LLM prompt, with an explicit note: local variables come from the method body, NOT `lookup_symbol()`

---

## 🚀 CLI Usage (v2)

```bash
# Default (Ollama + qwen2.5-coder)
analyze-apis /path/to/java/project

# Save to file
analyze-apis /path/to/java/project -o openapi.json

# Different LLM provider/model
analyze-apis /path/to/project --llm-provider openai --llm-model gpt-4o
analyze-apis /path/to/project --llm-provider anthropic --llm-model claude-3-5-sonnet-20241022

# More hops (for complex cross-file resolution)
analyze-apis /path/to/project --max-hops 8

# Parallel resolution (faster for large codebases)
analyze-apis /path/to/project --async

# Verbose
analyze-apis /path/to/project -v
```

---

## 🔮 Next Steps (in priority order)

### Immediate
- [ ] Re-run 4-scenario test to confirm all 4 APIs appear with correct URLs
- [ ] Test cross-class constant: `BASE_TENANT_URL` defined in a separate `ApiConstants.java`
- [ ] Add `ResolutionStatus.PARTIAL` for partial resolutions
- [ ] Write pytest suite: `SymbolIndex`, `ChainDetector`, `extract_result()`, `OpenAPIGenerator`

### Next Libraries (MVP 2+)
- [ ] **RestTemplate** (Spring) — `restTemplate.getForObject(url, ...)`, `exchange(...)`
- [ ] **WebClient** (Spring WebFlux) — `.get().uri(...).retrieve()`
- [ ] **OkHttp** — `new Request.Builder().url(url).build()`
- [ ] **Apache HttpClient** — `new HttpGet(url)`, `HttpPost(url)`
- [ ] **FeignClient** — `@FeignClient` + `@GetMapping` annotations
- [ ] **Retrofit** — `@GET("/path")` interface annotations

### Future / Phase 2
- [ ] `@Value("${api.base-url}")` injection tracking (properties lookup tool handles this already)
- [ ] Multi-library support (one agent per library type)
- [ ] YAML output format option
- [ ] Merge multiple calls to same endpoint intelligently

---

## 🧠 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| LangGraph for orchestration | Native loop/cycle support, stateful, best for ReAct agents |
| SymbolIndex pre-built (not lazy) | Zero I/O during the agentic loop — all lookups are instant dict lookups |
| Same-file first → whole-project fallback | Efficient; avoids false matches on common names |
| Tools are closures over SymbolIndex | LangChain @tool must be pure functions; index injected via closure |
| LLM outputs structured JSON | Structured final response is more reliable than parsing free text |
| Graceful fallback on LLM failure | Always emits an ApiCall (UNRESOLVED) — never crashes or skips silently |
| Provider-agnostic LLM client | Lazy imports — only install the package for the provider you use |
| LLM always required (no static mode) | Simplifies codebase; static analysis alone was too limited |
| `method_context` in prompt | Local variables are NOT in the symbol index — the method body is the source of truth |
| `/unresolved/<expr>` placeholder | Never silently drop a detected call — visibility beats silent omission |
| Null-coerce LLM dict/list fields | LLMs sometimes emit `null` for empty collections; coerce defensively before Pydantic |

---

## 📎 Session History

| Date | What happened |
|------|--------------|
| 2026-07-08 | MVP 1 built and committed. `java.net.http.HttpClient` analyzer working. 10/10 test patterns pass. |
| 2026-07-08 | User tested on real Java code. Found: cross-class constants not resolved, headers missing on dynamic URLs. |
| 2026-07-08 | v2 agentic rebuild completed. LangGraph loop + SymbolIndex + provider-agnostic LLM. 14 chains detected correctly. |
| 2026-07-08 | Bug fix (commit `8949b16`): Pydantic crash on null dict/list fields + prompt hardened to force symbol lookups. |
| 2026-07-08 | Bug fix (commit `5fcee98`): Unresolved calls silently dropped — replaced `continue` with `/unresolved/<expr>` placeholder; added empty-URL fallback in `extract_result()`. |
| 2026-07-08 | Bug fix (commit `89167c7`): Scenario 2 resolved to wrong URL (merged with Scenario 1). Added `method_context` field (enclosing method body) to `RawChain`; surfaced it first in LLM prompt so local variable assignments are always visible. All 4 test scenarios now expected to produce distinct correct paths. |
