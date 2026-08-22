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
council websites (ModernGov; ~80% of councils)
        │  scraped by
        ▼
  poteris/council-scraper (Rails + Sidekiq) → Postgres + OpenSearch + DO Spaces
        │  read by
        ▼
  poteris/council-api (FastAPI, read-only)  ──  /council-api  (REST, OpenAPI)
        │
        ▼
  lgsf-mcp (this repo)  ──  MCP tools  ──▶  Claude / other MCP clients
```

`council-scraper` is Poteris's older scraper; Democracy Club's LGSF is
superseding it. Today the API serves `council-scraper` data, so that is what
this MCP server talks to; expect the upstream source to switch to LGSF
output over time, and keep this server's coupling to the API (not the
scraper schema) so that change stays upstream.

## Resources

| Resource | URL | Notes |
| --- | --- | --- |
| Council Gateway API | <https://councilgateway.poteris.co.uk/council-api> | Read-only REST API. Bearer auth is a static `API_TOKENS` list; the live instance runs with `auth_disabled`, so no token is needed today. |
| council-api source | <https://github.com/poteris/council-api> | FastAPI over the scraper's Postgres + OpenSearch. Deployed on Render (Frankfurt). `app/models.py` mirrors the scraper schema. |
| council-scraper source | <https://github.com/poteris/council-scraper> | Poteris's older Rails/Sidekiq scraper of ModernGov sites; owns the current schema. ~1.59M documents in production. Being superseded by LGSF. |
| OpenAPI spec | <https://councilgateway.poteris.co.uk/council-api/openapi.json> | OpenAPI 3.1. 15 `GET` endpoints. No descriptions in spec. |
| Council Gateway dashboard | <https://councilgateway.poteris.co.uk/> | Alpha web UI over the same data. Source repo not publicly identified (not `council-api`, which is API-only). |
| LGSF | <https://github.com/DemocracyClub/LGSF> | Democracy Club's scraper framework, superseding `council-scraper`. One package per council (~440). Scrapes councillors, minutes, decisions. Alpha/unstable. |
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

Facts from the `council-api` README that matter for tool design:

- Only **document text** and **document classifications** are indexed in
  OpenSearch. Meetings, decisions and people are not searchable; reach them
  via filters or via the documents that mention them.
- `/search` is a `multi_match` phrase query (`slop: 10`) with highlighting;
  each hit is hydrated with its Postgres record.
- Documents are polymorphic: `source_type` / `source_id` point at a Meeting
  or a Decision.
- Meeting detail includes LLM-generated summaries (`topline`, `about`,
  `notes`); `/documents/{id}/classifications` lists LLM classification runs.
- `/documents/{id}/file` redirects to a presigned DigitalOcean Spaces URL.
- Council-wide filtered listings are slow in production (documents by
  council ~15s, `is_minutes` ~7s, key decisions ~3.5s) unless optional
  indexes have been added.

## Status

Planning. See [PLAN.md](PLAN.md).
