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

## ✅ v2 AGENTIC REBUILD — IN PROGRESS

### Architecture Decision
Moved from static one-pass analysis to a **LangGraph-based agentic loop**:
- LLM receives the raw builder chain + class context
- LLM calls tools if it needs to resolve unknown symbols/constants/properties
- Loop exits when LLM emits a final JSON block (or max hops reached)

### What works (as of this session):
- ✅ **ChainDetector** — detects all `HttpRequest.newBuilder()…build()` chains
- ✅ **FileScanner** — collects `.java` + `.properties`/`.yml` files
- ✅ **SymbolIndex** — pre-scans ALL Java files, indexes `static final` fields,
  class→file mappings, and `.properties`/YAML keys
- ✅ **LangGraph ResolverGraph** — `resolver_node` ↔ `tools_node` loop with hop limit
- ✅ **Tools**: `lookup_symbol`, `get_class_source`, `lookup_property`
- ✅ **ResolverAgent** — sync + async `resolve_all` / `resolve_all_async`
- ✅ **LLM client** — provider-agnostic factory (Ollama/OpenAI/Anthropic/Google)
- ✅ **Main CLI** — rebuilt, 5-step pipeline
- ✅ **Full import and detection smoke test passing** (14 chains detected correctly)

### What's pending:
- [ ] Run full end-to-end test with Ollama running (`qwen2.5-coder`)
- [ ] Validate OpenAPI output quality for cross-class constant scenarios
- [ ] Add `ResolutionStatus.PARTIAL` and `PROPERTIES_RESOLVED` status values
- [ ] Formal pytest test suite
- [ ] Git commit of v2 rebuild

---

## 🗂️ Module Structure (v2)

```
src/ai_agents/
├── main.py                              # CLI: analyze-apis (v2 agentic)
├── models/api_call.py                  # Pydantic ApiCall + ResolutionStatus (unchanged)
├── parsers/java_parser.py              # tree-sitter Java wrapper (unchanged)
├── scanner/
│   ├── file_scanner.py                 # Collect .java + .properties files
│   └── chain_detector.py               # tree-sitter chain detection → RawChain
├── index/
│   └── symbol_index.py                 # SymbolIndex + SymbolIndexBuilder
├── agents/
│   ├── tools/
│   │   └── resolver_tools.py           # @tool: lookup_symbol, get_class_source, lookup_property
│   ├── graph.py                        # LangGraph StateGraph (resolver ↔ tools loop)
│   └── resolver_agent.py              # Public interface: resolve(chain) → ApiCall
├── llm/client.py                       # Provider factory (Ollama/OpenAI/Anthropic/Google)
└── output/openapi.py                   # OpenAPI 3.1.0 generator (unchanged)
```

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
- [ ] Run with Ollama live to validate end-to-end JSON output quality
- [ ] Test `BASE_TENANT_URL` scenario resolves to full URL in OpenAPI output
- [ ] Add `ResolutionStatus.PARTIAL` for partial resolutions
- [ ] Write pytest suite for SymbolIndex + ChainDetector

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

---

## 📎 Session History

| Date | What happened |
|------|--------------|
| 2026-07-08 | MVP 1 built and committed. `java.net.http.HttpClient` analyzer working. 10/10 test patterns pass. |
| 2026-07-08 | User tested on real Java code. Found: cross-class constants not resolved, headers missing on dynamic URLs. |
| 2026-07-08 | v2 agentic rebuild completed. LangGraph loop + SymbolIndex + provider-agnostic LLM. 14 chains detected correctly. Pending: live Ollama test. |
| 2026-07-08 | Bug fix: (1) Pydantic `ValidationError` crash when LLM emits `null` for `query_params`/`cookies` — added `_as_dict`/`_as_list` coercion helpers in `extract_result()`. (2) Strengthened system prompt: LLM must call `lookup_symbol()` for ALL URL variables (e.g. `BASE_TENANT_URL`, `fullUrl`) before writing final answer; example JSON uses `{}` for empty objects to deter null emission. |
