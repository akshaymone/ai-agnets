# Project Context — ai-agents Java REST API Analyzer

> **Update this file at the end of every session.**
> When starting a new session, share this file with the AI to resume instantly.

---

## 🎯 Project Goal

Build a Python package (`ai-agents`) that statically analyzes a Java codebase
and detects all **outbound REST API calls**, then outputs an **OpenAPI 3.1.0** spec.

- Language scope: **Java only** (for now)
- Parsing engine: **tree-sitter** (via `tree-sitter-java` Python binding)
- LLM integration: **LangChain → Ollama** (local), swappable to OpenAI/Anthropic
- Output format: **OpenAPI 3.1.0 JSON**
- Architecture: **one analyzer module per Java HTTP library**, tested independently

---

## ✅ MVP 1 — DONE (commit `bfb98f1`)

### Library covered: `java.net.http.HttpClient` (Java 11+)

**What is detected:**
- HTTP method: GET, POST, PUT, DELETE, PATCH, HEAD, `.method("VERB", body)`
- URI from `.uri(URI.create("..."))` — literal and dynamic (variable-based)
- Headers from `.header("k","v")` and `.headers("k1","v1","k2","v2",...)`
- Cookies parsed from the `Cookie` header value
- Request body from `HttpRequest.BodyPublishers.ofString(...)` etc.
- Query parameters extracted from literal URLs
- Path parameters: exact from `{placeholder}` URLs, heuristic from dynamic URIs

**Resolution statuses:**
- `literal` — URL fully extracted as a string constant ✅
- `dynamic` — URL built from variables; path params are heuristic guesses ⚠️
- `llm_resolved` — LLM was used to normalize the dynamic URL ✅ (with `--llm` flag)

**Patterns tested in `tests/java_samples/ApiService.java`:**
1. Simple GET with literal URL + `Accept` header
2. GET with dynamic URL (`baseUrl + "/users/" + userId`) + `Authorization` header
3. GET with query params in literal URL (`?q=john&page=1&size=20`)
4. POST with literal JSON body + `Content-Type: application/json`
5. PUT with dynamic URL + JSON body
6. DELETE with dynamic URL
7. PATCH via `.method("PATCH", body)` + `Content-Type: application/json-patch+json`
8. GET with `Cookie` header (3 cookies parsed: session, user_id, csrf_token)
9. GET with `.headers(...)` multi-header shorthand
10. GET with URI passed directly to `newBuilder(URI.create(...))`

**Result: 10/10 patterns detected correctly.**

---

## 🗂️ Module Structure

```
src/ai_agents/
├── main.py                              # CLI: analyze-apis
├── models/api_call.py                  # Pydantic ApiCall + ResolutionStatus
├── parsers/java_parser.py              # tree-sitter Java wrapper
├── analyzers/
│   ├── base.py                         # BaseAnalyzer ABC
│   └── java_net_http/analyzer.py       # MVP 1 ← DONE
├── llm/client.py                       # LangChain LLM (Ollama default)
├── output/openapi.py                   # OpenAPI 3.1.0 generator
└── utils/file_scanner.py               # .java file scanner
```

---

## 🚀 CLI Usage

```bash
# Basic scan
analyze-apis /path/to/java/project

# Save to file
analyze-apis /path/to/java/project -o openapi.json

# With LLM (Ollama running locally)
analyze-apis /path/to/java/project --llm --llm-model llama3.2

# Verbose
analyze-apis /path/to/java/project -v
```

---

## 🔮 Next Steps (in priority order)

### Immediate (post-MVP-1 testing feedback)
- [ ] Fix `/{baseUrl}/users/{userId}` → LLM should strip class-level base URL variables
- [ ] Handle `String.format("https://api.example.com/%s/orders/%s", userId, orderId)`
- [ ] Handle `sendAsync()` (same patterns, different call site)
- [ ] Handle builder stored in a variable (multi-statement, not fluent chain)

### Next Libraries (MVP 2+)
- [ ] **RestTemplate** (Spring) — `restTemplate.getForObject(url, ...)`, `exchange(...)`
- [ ] **WebClient** (Spring WebFlux) — `.get().uri(...).retrieve()`
- [ ] **OkHttp** — `new Request.Builder().url(url).build()`
- [ ] **Apache HttpClient** — `new HttpGet(url)`, `HttpPost(url)`
- [ ] **FeignClient** — `@FeignClient` + `@GetMapping` annotations
- [ ] **Retrofit** — `@GET("/path")` interface annotations

### Future / Phase 2
- [ ] `.properties` / `.yml` file scanning for URL values
- [ ] `@Value("${api.base-url}")` injection tracking
- [ ] Merge multiple calls to same endpoint intelligently
- [ ] YAML output format option
- [ ] pytest test suite

---

## 🧠 Key Design Decisions Made

| Decision | Rationale |
|----------|-----------|
| One analyzer per library | Isolate complexity; test each library fully before next |
| tree-sitter AST (not regex) | Handles all code styles, indentation, multiline chains |
| LLM is optional (`--llm` flag) | Works without Ollama; LLM only for dynamic URL resolution |
| LangChain for LLM | Provider-agnostic; swap Ollama → OpenAI with one param change |
| OpenAPI 3.1.0 as output | Standard format, usable in Swagger UI / Postman / etc. |
| `ResolutionStatus` enum | Communicates confidence level of each detected URL |

---

## 📎 Session History

| Date | What happened |
|------|--------------|
| 2026-07-08 | MVP 1 built and committed. `java.net.http.HttpClient` analyzer working. 10/10 test patterns pass. Pushed to `akshaymone/ai-agnets` main branch. |
