# lgsf-mcp

An [MCP](https://modelcontextprotocol.io/) server exposing UK local
government data — councils, committees, meetings, minutes, decisions,
councillors — to LLM clients such as Claude Code and Claude Desktop.

## Goal

Let an LLM answer questions like "what did Sheffield's planning committee
decide about X last year?" or "which councils discussed Y in their minutes?"
by querying real scraped council data rather than guessing. The server is a
thin, well-described tool layer over the Council Gateway API; all heavy
lifting (scraping, extraction, indexing) happens upstream.

Initially search is full-text only (OpenSearch). A later phase will add
semantic search backed by a vector database so the model can find relevant
minutes by meaning rather than keyword. The tool interface is designed so
that can be added without breaking existing clients.

## Data pipeline

```
council websites (ModernGov, CMIS, bespoke HTML)
        │  scraped by
        ▼
  LGSF — Local Government Scraper Framework
        │  loaded into
        ▼
  Council Gateway (Postgres + OpenSearch)  ──  /council-api  (REST, OpenAPI)
        │
        ▼
  lgsf-mcp (this repo)  ──  MCP tools  ──▶  Claude / other MCP clients
```

## Resources

| Resource | URL | Notes |
| --- | --- | --- |
| Council Gateway API | <https://councilgateway.poteris.co.uk/council-api> | Read-only REST API. Currently no auth required despite spec declaring bearer. |
| OpenAPI spec | <https://councilgateway.poteris.co.uk/council-api/openapi.json> | OpenAPI 3.1. 15 `GET` endpoints. No descriptions in spec. |
| Council Gateway dashboard | <https://councilgateway.poteris.co.uk/> | Alpha web UI over the same data. Source repo not publicly identified. |
| LGSF | <https://github.com/DemocracyClub/LGSF> | Democracy Club's scraper framework. One package per council (~440). Scrapes councillors, minutes, decisions. Alpha/unstable. |
| lgsf-dashboard | <https://github.com/DemocracyClub/lgsf-dashboard> | Eleventy static site showing LGSF scraper run logbooks (pass/fail per council). Not the Council Gateway UI. |
| Democracy Club Slack | <https://slack.democracyclub.org.uk/> | Recommended by LGSF README for anyone using the data. |

## API surface (upstream)

| Resource | Endpoints | Key filters |
| --- | --- | --- |
| Councils | `/councils`, `/councils/{id}`, `/councils/{id}/committees` | `council_type` |
| Meetings | `/meetings`, `/meetings/{id}` | `council_id`, `committee_id`, `date_from`, `date_to`, `has_minutes` |
| Documents | `/documents`, `/documents/{id}`, `/documents/{id}/classifications`, `/documents/{id}/file` | `meeting_id`, `decision_id`, `council_id`, `is_minutes`, `kind`, `processing_status`; `include_text` on detail |
| Decisions | `/decisions`, `/decisions/{id}` | `council_id`, `date_from`, `date_to`, `is_key` |
| People | `/people`, `/people/{id}` | `council_id`, `is_councillor`, `party` |
| Search | `/search` | `q` (required), `council_id`, `mode` = `document` \| `classification`, `size`, `offset` |

All list endpoints paginate with `page` / `per_page` (max 100) and return
`{items, page, per_page, total}`. Search paginates with `size` / `offset`.

## Status

Planning. See [PLAN.md](PLAN.md).
