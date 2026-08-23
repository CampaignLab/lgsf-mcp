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

Working. 14 tools over the Council Gateway API, stdio and streamable-HTTP
transports, optional static bearer auth on HTTP, and an opt-in live test
suite against the real API. See [PLAN.md](PLAN.md).

## Usage

### Install

```bash
uv sync
```

### Claude Code (stdio)

Add to `.mcp.json` in the project where you want the tools available:

```json
{"mcpServers": {"lgsf": {"command": "uv", "args": ["run", "--directory", "/path/to/lgsf-mcp", "lgsf-mcp"]}}}
```

`--directory` makes the launch independent of the client's working
directory. stdio is the default transport, so no flags are needed.

Environment variables (all optional):

| Variable | Effect |
| --- | --- |
| `COUNCIL_API_URL` | Upstream API base URL. Defaults to `https://councilgateway.poteris.co.uk/council-api`. Point it at a local gateway instance if you have one. |
| `COUNCIL_API_TOKEN` | Sent upstream as `Authorization: Bearer`. The live API currently runs with auth disabled, so this is not needed today. |
| `LGSF_MCP_TOKEN` | Shared secret required on *inbound* HTTP requests. Ignored by stdio (which warns on stderr if it is set). |
| `LGSF_LIVE` | Set to `1` to opt into the live tests that hit the real API. |

### HTTP transport

```bash
uv run lgsf-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The endpoint is `/mcp` — so `http://127.0.0.1:8000/mcp` with those defaults.
`--host` and `--port` default to `127.0.0.1` and `8000`.

Set `LGSF_MCP_TOKEN` to require a bearer token on every request:

```bash
LGSF_MCP_TOKEN=some-long-random-string \
  uv run lgsf-mcp --transport streamable-http --port 8000
```

Requests without a valid `Authorization: Bearer <token>` then get `401` with
a `WWW-Authenticate` header, and the server publishes RFC 9728 metadata at
`/.well-known/oauth-protected-resource/mcp`. Without the variable it serves
unauthenticated. The token is read from the environment rather than a flag so
it stays out of `ps` output and shell history.

`--public-url` sets the externally reachable origin advertised in that
metadata (default `http://HOST:PORT`). It is only used when `LGSF_MCP_TOKEN`
is set.

**Client compatibility — the key limitation.** Claude.ai custom connectors and
ChatGPT connectors can only speak OAuth or no-auth; **neither can be told to
send a fixed `Authorization` header**. So `LGSF_MCP_TOKEN` protects Claude
Code, `curl` and scripted clients, but *not* those two. Exposing this server
to Claude.ai or ChatGPT today means exposing it unauthenticated. Real OAuth is
tracked separately — see [PLAN.md](PLAN.md), *Protecting the HTTP endpoint*,
option 3.

ChatGPT additionally requires tools named exactly `search` and `fetch`; this
server provides both, alongside the richer Claude-facing tools.

### Public testing via Tailscale Funnel

[Funnel](https://tailscale.com/kb/1223/funnel) puts a local port on a public
HTTPS URL, which is enough to try the server from Claude.ai or ChatGPT without
deploying anything. Prerequisites: MagicDNS enabled, HTTPS certificates
enabled, and a `funnel` node attribute in your tailnet policy file.

Run the server, then, in another shell:

```bash
tailscale funnel 8000
```

That serves it at `https://<machine>.<tailnet>.ts.net`, and the MCP endpoint
is `https://<machine>.<tailnet>.ts.net/mcp`. Add that URL as a custom
connector in Claude.ai (Settings → Connectors → Add custom connector) or as a
connector in ChatGPT. Funnel's public listener can only use ports 443, 8443
and 10000; the local target port is unrestricted, so proxying local 8000 is
fine.

**Gotcha:** if you set `LGSF_MCP_TOKEN`, you must also pass the Funnel URL as
`--public-url`, or the advertised metadata points at a loopback address and
clients fail with a resource mismatch:

```bash
LGSF_MCP_TOKEN=some-long-random-string \
  uv run lgsf-mcp --transport streamable-http --port 8000 \
  --public-url https://<machine>.<tailnet>.ts.net
```

Traffic over a Funnel is subject to non-configurable bandwidth limits, so
treat this as a testing path, not a deployment.

### Tests

```bash
uv run pytest                                  # recorded fixtures; no network
LGSF_LIVE=1 uv run pytest tests/test_live.py   # hits the real API
```

The live tests are skipped unless `LGSF_LIVE=1`. Quality gates, all of which
must pass:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## Licence

[GNU Affero General Public License v3.0 or later](LICENSE)
(`AGPL-3.0-or-later`). This server is intended to be run as a network
service, so AGPL section 13 applies: anyone offering it over a network must
offer its users the corresponding source.
