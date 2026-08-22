# Plan

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Language | Python ≥ 3.12 | Matches LGSF / DC ecosystem; `uv` for deps. |
| MCP framework | `mcp` (official SDK, `FastMCP` class) | Decorator-based tools, schema from type hints. |
| HTTP client | `httpx` | Async, typed, easy to mock. |
| Transport | stdio **and** streamable HTTP (`--transport` flag) | stdio for Claude Code/Desktop. HTTP for testing from Claude.ai / ChatGPT via a public URL (Tailscale Funnel). |
| Public test URL | Tailscale Funnel | `tailscale funnel 8000` → `https://<machine>.<tailnet>.ts.net`. Funnel (not Serve) is needed: Anthropic/OpenAI servers are not on the tailnet. No auth for testing; upstream is open and read-only anyway. |
| ChatGPT compatibility | `search` + `fetch` tools in OpenAI's required shape | ChatGPT connectors require exactly these; Claude does not care. Rich tools keep distinct names. |
| Tool definitions | Hand-written | Spec has zero descriptions; generated tools would be unusable by an LLM. 15 endpoints is small. |
| Auth | Optional `COUNCIL_API_TOKEN` env var | API is currently open; send `Authorization: Bearer` only if set. |
| Base URL | `COUNCIL_API_URL` env var, default `https://councilgateway.poteris.co.uk/council-api` | Lets a local gateway instance be used. |

## Layout

```
pyproject.toml              # uv project; script entry point `lgsf-mcp`
src/lgsf_mcp/
  __init__.py
  client.py                 # CouncilGatewayClient: one method per endpoint, returns dicts
  server.py                 # FastMCP instance + tool functions
  search.py                 # search backends (text now; semantic later)
  text.py                   # helpers: truncation / windowing of large document text
tests/
  test_client.py            # httpx MockTransport against recorded fixtures
  test_tools.py             # tool-level tests
  test_live.py              # opt-in smoke tests against the real API (skipped unless LGSF_LIVE=1)
```

## Tools

Names are verbs the model will reach for. Every tool gets a description that
explains the data model, since the upstream spec has none.

| Tool | Upstream | Params | Notes |
| --- | --- | --- | --- |
| `search_council_data` | `/search` | `q`, `council_id?`, `mode=document\|classification`, `size`, `offset` | Primary entry point for Claude. Returns hits with highlights and `record`. Description tells the model to follow up with `get_document` / `get_meeting`. |
| `search` | `/search` | `query` | ChatGPT-shaped alias: returns `{results: [{id, title, url}]}`. `id` is the upstream hit id (e.g. `Document-1432934`). |
| `fetch` | `/documents/{id}` or `/meetings/{id}` by id prefix | `id` | ChatGPT-shaped alias: returns `{id, title, text, url, metadata}`. Text is windowed like `get_document`. |
| `list_councils` | `/councils` | `council_type?`, `page`, `per_page` | ~440 councils; description says to page or to search by name client-side. |
| `get_council` | `/councils/{id}` + `/councils/{id}/committees` | `council_id` | Merges committees into response — saves a round trip. |
| `list_meetings` | `/meetings` | `council_id?`, `committee_id?`, `date_from?`, `date_to?`, `has_minutes?`, `page`, `per_page` | Dates ISO `YYYY-MM-DD`. |
| `get_meeting` | `/meetings/{id}` | `meeting_id` | Already embeds decisions, documents, attendees, tags. |
| `list_documents` | `/documents` | `meeting_id?`, `decision_id?`, `council_id?`, `is_minutes?`, `kind?`, `processing_status?`, `page`, `per_page` | |
| `get_document` | `/documents/{id}?include_text=` | `document_id`, `include_text=false`, `text_offset=0`, `max_chars=20000` | Minutes text can be very large; window it client-side and report `text_length` + `has_more`. |
| `get_document_file_url` | `/documents/{id}/file?redirect=false` | `document_id` | Returns the URL, never bytes. |
| `list_decisions` | `/decisions` | `council_id?`, `date_from?`, `date_to?`, `is_key?`, `page`, `per_page` | |
| `get_decision` | `/decisions/{id}` | `decision_id` | |
| `list_people` | `/people` | `council_id?`, `is_councillor?`, `party?`, `page`, `per_page` | |
| `get_person` | `/people/{id}` | `person_id` | |

Not exposed: `/healthz`; `/documents/{id}/classifications` (add as a flag on
`get_document` if it proves useful).

Return values are plain JSON-serialisable dicts. Pagination metadata
(`total`, `page`, `per_page`) is passed through so the model knows whether to
fetch more. Upstream 422s are surfaced as tool errors with the validation
detail.

## Phases

### 1. Scaffold and client

- `uv init`, pyproject with `mcp`, `httpx`; dev deps `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
- `client.py` with one async method per endpoint, shared `_get()` that adds
  optional bearer header and raises on non-2xx.
- Record a few live responses into `tests/fixtures/` for mocked tests.

### 2. Tools

- Implement the tools above in `server.py`.
- Write descriptions explaining: what a council/meeting/document/decision
  is; that documents hang off meetings *or* decisions (`source_type` /
  `source_id`); that `is_minutes` documents carry the extracted text; that
  `search` is the right starting point for open-ended questions.
- Text windowing helper for `get_document`.

### 3. Testing and integration

- Unit tests via `httpx.MockTransport`.
- Opt-in live smoke tests (`LGSF_LIVE=1`).
- `.mcp.json` snippet in README for Claude Code; `uv run lgsf-mcp` entry point.
- `uv run lgsf-mcp --transport http --port 8000` binds `127.0.0.1:8000`;
  expose with `tailscale funnel 8000` and add the `.ts.net` URL as a custom
  connector in Claude.ai and ChatGPT. Prereqs: MagicDNS, HTTPS certs, `funnel`
  nodeAttr in tailnet policy; Funnel allows ports 443/8443/10000 only.
- Verify ChatGPT's transport expectation (docs show `/sse/` URLs) and whether
  developer mode relaxes the `search`/`fetch` requirement; enable SSE too if
  needed.
- Manual check: ask Claude Code a few realistic questions and refine
  descriptions based on how it actually uses the tools.

### 4. Semantic search (future)

Not in scope yet, but the design leaves room:

- `search.py` defines a `SearchBackend` protocol with one implementation
  today (`TextSearchBackend` → `/search`).
- The `search_council_data` tool gains a `method` parameter (`text` | `semantic` |
  `hybrid`, default `text`). Adding a method does not break existing callers.
- Result shape is shared: each hit carries `document_id` (and `meeting_id`
  where known) so the model can call `get_document` regardless of backend.

Open questions to settle before starting phase 4:

- **Where do embeddings live?** Options: (a) upstream Council Gateway adds a
  `/search?mode=semantic` endpoint and this server stays thin — preferred if
  Poteris/DC are willing; (b) this server builds and hosts its own index
  (e.g. pgvector, LanceDB, or Chroma) by paging `/documents?is_minutes=true`
  with `include_text` and embedding chunks locally. (b) means a batch
  indexing job, storage, and keeping up with new scrapes.
- **Chunking.** Minutes are long; embed per agenda item or fixed-size chunks
  with overlap, storing `document_id` + char offsets so `get_document` can
  jump straight to the relevant window via `text_offset`.
- **Embedding model.** Local (e.g. `bge-small`, `nomic-embed`) vs hosted
  API. Cost/latency trade-off; decide once corpus size is known
  (`/search` reports `total: 10000` for a common term, i.e. capped, so true
  corpus size needs measuring via `/documents` totals).
- **Hybrid ranking.** If both backends exist, reciprocal rank fusion over
  text + vector results is the simplest thing that works.
