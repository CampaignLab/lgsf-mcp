# OAuth 2.1 spike: choosing an authorization server

Research spike for bead `lgmcp-o81.1`, first child of epic `lgmcp-o81`.
**This is a decision document, not an implementation.** Its purpose is to let
the build beads be written against verified facts.

Spike date: 2026-08-23. Verified against `mcp` **2.0.0** as installed in this
worktree, and MCP specification revision **2026-07-28**.

---

## TL;DR

**Recommendation: stay a pure OAuth 2.1 resource server, and point
`issuer_url` at [WorkOS AuthKit](https://workos.com/docs/authkit/mcp) as the
authorization server.** It is the only candidate verified to support *both*
Client ID Metadata Documents (the mechanism the current spec prefers) and
Dynamic Client Registration (the deprecated fallback), on a free tier that
comfortably covers a hobby project, with no server to patch.

Two findings should change how this work is scoped:

1. **The premise this spike was commissioned under is out of date.** DCR is no
   longer a `SHOULD`; the 2026-07-28 spec explicitly **deprecates** it in
   favour of Client ID Metadata Documents, and ranks a **pre-registered
   client first**. See [Correction](#correction-the-dcr-centric-framing-is-out-of-date).
2. **RFC 8707 audience validation is a spec `MUST` that the SDK does not do
   for us.** Nothing in the SDK's request path reads `AccessToken.resource`.
   Shipping without our own check would be non-compliant and would reintroduce
   the confused-deputy bug RFC 8707 exists to prevent. See
   [The one thing we must not get wrong](#the-one-thing-we-must-not-get-wrong).

---

## Correction: the DCR-centric framing is out of date

The brief for this spike stated that "the MCP spec says clients SHOULD use
[Dynamic Client Registration]". **That was true of an earlier revision and is
no longer correct.** Recording it explicitly so it is not reintroduced later.

MCP specification 2026-07-28, [Authorization §Overview][spec-authz]:

> 2. Authorization servers and MCP clients **SHOULD** support [OAuth Client ID
>    Metadata Documents] ([draft-ietf-oauth-client-id-metadata-document-00]).
>
> 3. Authorization servers and MCP clients **MAY** support the OAuth 2.0
>    Dynamic Client Registration Protocol ([RFC7591]). Note that Dynamic
>    Client Registration is deprecated and retained for backwards
>    compatibility with authorization servers that do not support Client ID
>    Metadata Documents.

The [Client Registration][spec-reg] page carries an explicit warning:

> Dynamic Client Registration is deprecated. New implementations should use
> Client ID Metadata Documents instead. This option remains available for
> backwards compatibility with authorization servers that do not support
> Client ID Metadata Documents.

And defines the selection priority — note that **pre-registration ranks
first**, ahead of both CIMD and DCR:

> Clients supporting all options **SHOULD** use the following priority order:
>
> 1. Use pre-registered client information for the server if the client has it available
> 2. Use Client ID Metadata Documents if the Authorization Server indicates that it supports them (via `client_id_metadata_document_supported` in OAuth Authorization Server Metadata)
> 3. Use Dynamic Client Registration as a fallback if the Authorization Server supports it (via `registration_endpoint` in OAuth Authorization Server Metadata)
> 4. Prompt the user to enter the client information if no other option is available

### Why this materially changes the decision

The brief treated "does this AS support DCR?" as the gate on AS selection.
It is not. Three consequences:

- **A manually pre-registered client is spec-blessed priority 1**, not a
  second-best hack. For a single-maintainer project with a handful of known
  users, pasting a client ID and secret into a connector dialog once is a
  legitimate terminal state, not a stopgap.
- **CIMD costs us nothing to support.** It is a *client-side* capability: the
  client hosts a JSON document at an HTTPS URL and uses that URL as its
  `client_id`; the AS fetches and validates it. As a resource server we
  neither host nor validate it. So CIMD is a criterion for picking the **AS**
  and adds zero work on our side.
- **The Claude.ai unknown stops being fatal.** Because pre-registration is
  priority 1 and Claude.ai exposes Client ID/Secret fields, we have a working
  path even if Claude.ai supports neither CIMD nor DCR — which, see below, we
  could not determine.

[spec-authz]: https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
[spec-reg]: https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration

---

## 1. Who are the users?

The bead asks this first, and it drives everything.

**Assumption, stated so it can be challenged: the audience is the maintainer
plus a small number of known people** — collaborators, a few researchers or
journalists interested in UK local-government data. Not a public multi-tenant
service.

The upstream Council Gateway data is open and read-only, so authorization is
protecting **bandwidth, upstream rate limits and abuse surface, not secrets**.
Upstream is slow (council-wide document listings up to ~15s) and rate-limited,
so an unauthenticated public endpoint mainly risks becoming a free proxy that
burns someone else's API budget.

This justifies optimising for **low operational burden and zero cost** over
scalability or self-sovereignty. If the endpoint later becomes genuinely
public, revisit — that would raise the value of user-visible branding and of
owning the login experience.

---

## 2. Which authorization server?

### The hard constraint

We want to run as a **pure resource server** (`token_verifier=` only). In that
mode the SDK mounts exactly two routes and no OAuth endpoints of its own —
verified empirically, see [SDK fact 4](#fact-4-rs-mode-serves-exactly-two-routes-verified-by-running-it).
So the AS must be somebody else's, and **every** client-registration mechanism
has to come from that AS. We cannot paper over a gap in it.

### Candidates

| Option | CIMD | DCR | Cost at our scale | Ops burden | Verdict |
|---|---|---|---|---|---|
| **WorkOS AuthKit** | Yes (opt-in) | Yes (opt-in) | Free tier | None | **Recommended** |
| Stytch | Not verified | Yes | Free tier | None | Viable runner-up |
| Descope | Yes | Yes | Not verified | None | Viable |
| Auth0 | Not verified | Paid plans only | DCR needs Professional+ | None | Rejected |
| Okta | Not verified | No | Free integrator plan | None | Rejected |
| Google / GitHub direct | Not documented | Not documented | Free | None | Rejected |
| Self-hosted (SDK AS mode) | No | Yes | Free | High | Rejected |
| Keycloak self-hosted | Not verified | Yes | Free | High | Rejected |

### Recommended: WorkOS AuthKit

[WorkOS documents MCP support explicitly][workos-mcp], and is the one
candidate verified to offer **both** registration mechanisms, with CIMD
correctly identified as the spec-preferred one:

> The MCP protocol specifies that authorization servers should support Client
> ID Metadata Document (CIMD) to allow MCP clients to identify themselves, and
> this feature is off by default but can be enabled in the WorkOS Dashboard.
> You can enable the previous approach, Dynamic Client Registration (DCR), in
> the WorkOS Dashboard for backwards compatibility with MCP clients that don't
> yet support Client ID Metadata Document.
> — [WorkOS AuthKit MCP docs][workos-mcp]

Why it wins:

- **Covers all three registration paths.** CIMD for spec-current clients, DCR
  for laggards, pre-registration always available. Given we could not verify
  what Claude.ai does, covering every path is worth a lot — it converts an
  unknown into a non-issue.
- **Nothing to operate.** No server, no patching, no open registration
  endpoint of ours facing the internet.
- **Free tier suits the assumed scale.** ⚠️ Confirm current limits against
  the [pricing page][workos-pricing] before committing — pricing changes and
  is not something to take on trust from a spike document.
- **Survives a rebuild.** Logins live in WorkOS, so rebuilding or moving the
  Funnel host does not invalidate anyone's account — unlike a self-hosted AS
  whose user database is one bad `docker volume rm` from gone.

⚠️ **Not verified:** I did not create a WorkOS account or run the flow
end-to-end. The CIMD/DCR claims come from WorkOS's own documentation, not from
my own observation. First build bead should prove it before anything is built
on it.

[workos-mcp]: https://workos.com/docs/authkit/mcp
[workos-pricing]: https://workos.com/pricing

### Rejected, with reasons

**Auth0** — DCR is gated behind paid plans. Reported to require at minimum a
Professional or Enterprise plan plus a tenant-settings flag and pre-configured
third-party-app permissions. ⚠️ This comes from
[secondary sources][auth0-dcr] rather than an Auth0 pricing page I read
directly; treat as strong indication, not proof. Even discounted, there is no
reason to take the risk when free alternatives document MCP support outright.

**Okta** — Okta's own help documentation states it supports only
pre-registered OAuth clients for MCP and that DCR is not supported. Workable
via pre-registration, but strictly worse than WorkOS with no compensating
advantage for our use case.

**Google / GitHub used directly** — no official documentation could be found
stating that either supports RFC 7591 DCR, and neither is an OAuth 2.1 AS that
will mint audience-bound access tokens for *our* resource. Using them means
building an OAuth **proxy** — our server presenting a DCR/CIMD face to MCP
clients while holding one fixed upstream credential. That is a documented
community pattern ([FastMCP's OAuth Proxy][fastmcp-proxy], Cloudflare's
Workers pattern), but it means writing and owning an authorization server,
which is the thing we are trying to avoid. Rejected on burden, not on
feasibility.

**Self-hosting an AS (SDK `auth_server_provider=` mode)** — technically still
possible; the parameter survives in 2.0.0 and the SDK ships a complete RFC
7591 registration endpoint (facts [3](#fact-3-auth_server_provider-still-exists-in-200)
and [6](#fact-6-the-sdk-does-ship-a-real-dcr-endpoint--but-only-in-as-mode)).
Rejected because it means implementing the full
`OAuthAuthorizationServerProvider` protocol — roughly twelve methods spanning
authorization codes, refresh tokens and revocation — **plus** user
authentication, credential storage and session security. That is a
security-critical component with a permanent patching obligation, for a hobby
project, to replace something available free. It also means exposing an
**unauthenticated** `/register` endpoint that mints credentials
(`register.py` applies no authentication, as RFC 7591 permits), making abuse
mitigation our problem.

**Keycloak self-hosted** — same objection, plus a documented operational trap:
anonymous DCR allows unlimited client creation and the default realm threshold
of 300 clients is exhaustible by a trivial DoS. Sensible for an organisation
with an ops team; disproportionate here.

[auth0-dcr]: https://stytch.com/blog/mcp-oauth-dynamic-client-registration/
[fastmcp-proxy]: https://gofastmcp.com/servers/auth/oauth-proxy

---

## 3. Do the browser clients actually drive this flow?

### ChatGPT — verified, supports both mechanisms

[OpenAI's MCP documentation][openai-mcp] states:

> We recommend using OAuth with Client ID Metadata Documents (CIMD) for client
> registration when your authorization server supports CIMD. ChatGPT supports
> CIMD with public-client token exchange (`none`) or signed client assertion
> token exchange (`private_key_jwt`).

> Dynamic client registration remains supported when configured.

So ChatGPT drives CIMD *or* DCR. Against a WorkOS AS with both enabled, this
should work without pre-registration.

⚠️ Note the page distinguishes two different things, and it is easy to
conflate them: the **developer API** (Responses API MCP tool), where a
developer passes an authorization token directly in the API call, versus
**ChatGPT product connectors**, where an end user adds a connector through the
UI and cannot supply an arbitrary header. Only the latter is our situation.

[openai-mcp]: https://developers.openai.com/api/docs/mcp

### Claude.ai — NOT VERIFIED, and the docs are genuinely silent

This is the largest open question in the spike, and I am flagging it as
unknown rather than inferring an answer.

[Anthropic's custom-connector article][claude-connectors] says only:

> Optionally, click "Advanced settings" to specify an OAuth Client ID and
> OAuth Client Secret for your server.

> When you add a custom connector to Claude, you'll typically go through an
> OAuth authentication process to securely sign in to the application and
> grant specific permissions.

**What the documentation does not state** — I fetched the page directly to
confirm each of these is absent, rather than assuming:

- What happens when the Client ID/Secret fields are left **blank**. It does
  not say DCR is attempted.
- Whether Claude.ai supports **CIMD** at all.
- Which **grant types** it drives.

It is tempting to read "optional client credentials" as implying "DCR when
omitted". That is a plausible inference and it may well be right, **but the
documentation does not say it**, and building on an unverified inference is
exactly the failure mode this spike exists to prevent.

**Why the recommendation survives the unknown.** Those Advanced-settings
fields *are* the spec's registration mechanism #1 (pre-registration) — the
top-priority mechanism. So the worst case is: create one client in the WorkOS
dashboard by hand, paste its ID and secret into Claude.ai once, and the
connector works. **The recommendation deliberately does not depend on
Claude.ai performing DCR or CIMD.** If it turns out to do either, that is a
convenience win, not a prerequisite.

[claude-connectors]: https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp

---

## 4. Verified SDK facts

Every claim below is from the installed source at
`.venv/lib64/python3.13/site-packages/mcp/`, at `mcp` **2.0.0**. Paths are
given relative to that directory.

### Fact 1: `AuthSettings` fields

`server/auth/settings.py:15-42`:

| Field | Type | Default | Line |
|---|---|---|---|
| `issuer_url` | `AnyHttpUrl` | **required** | 22 |
| `service_documentation_url` | `AnyHttpUrl \| None` | `None` | 26 |
| `client_registration_options` | `ClientRegistrationOptions \| None` | `None` | 27 |
| `revocation_options` | `RevocationOptions \| None` | `None` | 28 |
| `required_scopes` | `list[str] \| None` | `None` | 29 |
| `identity_assertion_enabled` | `bool` | `False` | 30 |
| `resource_server_url` | `AnyHttpUrl \| None` | **required** (`Field(...)`) | 38 |

- `resource_server_url` is typed optional but declared `Field(...)` — i.e.
  **required but nullable** (line 38).
- `model_config = ConfigDict(url_preserve_empty_path=True)` (line 20) confirms
  the `model_validate` rule already encoded in `src/lgsf_mcp/auth.py`. The
  in-source comment cites RFC 8414/9207 exact-string issuer comparison as the
  reason.
- `identity_assertion_enabled` (lines 30-35) is **new in 2.0** and was not
  previously known to this project. It advertises the SEP-990 Identity
  Assertion Authorization Grant for enterprise IdP flows. Not needed here.
- `ClientRegistrationOptions` (lines 4-8) and `RevocationOptions` (11-12) are
  **AS-side** options. See fact 7 — they do nothing in RS mode.

### Fact 2: audience validation is NOT enforced by the SDK

Only two things gate a request:

1. `BearerAuthBackend.authenticate`, `server/auth/middleware/bearer_auth.py:47-66`
   — extracts the `Bearer ` header, calls `verify_token`, rejects on `None`,
   rejects if `auth_info.expires_at < now` (63-64). **That is all.**
2. `RequireAuthMiddleware.__call__`, `bearer_auth.py:93-111` — 401 if no
   `AuthenticatedUser` in scope; then loops `required_scopes` and returns 403
   `insufficient_scope` if any is missing (103-109).

`AccessToken.resource` exists at `server/auth/provider.py:57`, annotated
`# RFC 8707 resource indicator` — but **nothing in the request path ever reads
it.** There is no comparison of `resource` against `resource_server_url`
anywhere in the middleware.

### Fact 3: `auth_server_provider=` still exists in 2.0.0

Not removed, contrary to what the brief flagged as possible:

- `MCPServer.__init__(..., auth_server_provider=None, token_verifier=None, ...)`
  — `server/mcpserver/server.py:157-158`.
- Mutual exclusion at `server.py:237-242`: both → `ValueError("Cannot specify
  both auth_server_provider and token_verifier")`; neither, with `auth` set →
  `ValueError("Must specify either ... when auth is enabled")`; either without
  `auth` → `ValueError("Cannot specify auth_server_provider or token_verifier
  without auth settings")`. This confirms the "both must be given together"
  rule from `lgmcp-07y.1`.
- A provider is wrapped as `ProviderTokenVerifier(auth_server_provider)`
  (`server.py:248-249`), described at `provider.py:358-363` as "provided for
  backwards compatibility".

### Fact 4: RS mode serves exactly two routes (verified by running it)

Prototype (`tmp/oauth_spike.py`, throwaway, not committed) built
`MCPServer(token_verifier=..., auth=AuthSettings.model_validate({...}))` and
drove `streamable_http_app()` with `starlette.testclient.TestClient`.

Routes registered: **`/mcp` and `/.well-known/oauth-protected-resource/mcp`.
Nothing else.**

`GET /.well-known/oauth-protected-resource/mcp` → `200`, with
`Cache-Control: public, max-age=3600`:

```json
{
  "resource": "https://lgsf-mcp.tail1234.ts.net/mcp",
  "authorization_servers": ["https://lgsf.example.auth0.com"],
  "scopes_supported": ["lgsf:read"],
  "bearer_methods_supported": ["header"]
}
```

Unauthenticated `POST /mcp` → `401`:

```http
WWW-Authenticate: Bearer error="invalid_token",
                  error_description="Authentication required",
                  resource_metadata="https://lgsf-mcp.tail1234.ts.net/.well-known/oauth-protected-resource/mcp"
```

`GET /.well-known/oauth-authorization-server` → **`404`**. Expected and
important: in RS mode the SDK serves no AS metadata. That document is the
AS's responsibility.

Corollaries:

- No trailing slash on either URL — `model_validate` did its job.
- `scopes_supported` is populated from `required_scopes`
  (`server/lowlevel/server.py:821`). The two are the same list: **you cannot
  advertise a scope you do not also require.** Worth knowing before designing
  a scope scheme.
- `authorization_servers` is exactly `[issuer_url]` — a hardcoded
  single-element list (`server/lowlevel/server.py:820`). `AuthSettings` offers
  no way to advertise multiple authorization servers.
- The `resource_metadata=` pointer is built by `build_resource_metadata_url`
  (`server/auth/routes.py:202-217`), splicing
  `/.well-known/oauth-protected-resource` between host and path per RFC 9728
  §3.1.

### Fact 5: `IntrospectionTokenVerifier` is NOT in the installed wheel

`find .venv -path '*simple-auth*'` returns nothing. Grepping
`IntrospectionTokenVerifier` across `site-packages/mcp/` hits **only a
docstring reference** at `provider.py:363`. `bearer_auth.py:34` likewise
points at `examples/servers/simple-auth/mcp_simple_auth/token_verifier.py` —
**a path in the SDK's git repository, not in the distributed package.**

It lives at
[`examples/servers/simple-auth/`](https://github.com/modelcontextprotocol/python-sdk/tree/main/examples/servers/simple-auth)
in the upstream repo. Consequence: **there is no importable introspection
verifier.** If we introspect, we write it ourselves (~40 lines). Not a
blocker, but it needs its own bead and its own tests rather than being assumed
free.

`AccessToken` fields available to a verifier (`provider.py:52-59`): `token`,
`client_id`, `scopes`, `expires_at`, `resource`, `subject`, `claims`.
`claims` is where `iss` lives — `principal_components` (`provider.py:62-70`)
reads `claims["iss"]`.

### Fact 6: the SDK does ship a real DCR endpoint — but only in AS mode

`server/auth/handlers/register.py` is a complete RFC 7591 §3.1 implementation
(line 33 cites the RFC): mints `client_id = str(uuid4())` (48) and
`client_secret = secrets.token_hex(32)` (69); defaults
`token_endpoint_auth_method` to `client_secret_post` (51-52); **refuses
`private_key_jwt`** (57-60); enforces `valid_scopes`/`default_scopes` (71-82);
handles `client_secret_expires_at` per RFC 7591 §3.2.1 (121-127).

**But `/register` is mounted only when an `auth_server_provider` is
configured** — `create_auth_routes(...)` is called only under
`if auth_server_provider:` (`server/lowlevel/server.py:780-790`), and within
it `/register` additionally requires `client_registration_options.enabled`
(`server/auth/routes.py:120-134`).

It also applies **no authentication** to the registration endpoint —
anonymous registration, as RFC 7591 permits.

### Fact 7: registration and revocation options are silently inert in RS mode

Verified empirically (`tmp/dcr_check.py`, throwaway). Built `AuthSettings`
with **both** `client_registration_options={"enabled": True}` and
`revocation_options={"enabled": True}`, passed `token_verifier=` with no
`auth_server_provider`, and listed the routes:

```text
RS mode, registration+revocation ENABLED in AuthSettings:
    /mcp
    /.well-known/oauth-protected-resource/mcp
```

**No `/register`. No `/revoke`. No warning, no error.**

This is a real footgun: someone could set `client_registration_options.enabled
= True`, believe DCR is on, ship, and only discover at connector-setup time
that no registration endpoint exists. Worth a comment in `auth.py` and a test
asserting the route list, so that a future SDK change which starts honouring
the flag does not slip past unnoticed.

---

## The one thing we must not get wrong

**We must validate the token audience ourselves.** This combines fact 2 (the
SDK never reads `AccessToken.resource`) with a spec `MUST`.

[Authorization §Token Handling][spec-authz]:

> MCP servers, acting in their role as an OAuth 2.1 resource server, **MUST**
> validate access tokens as described in [OAuth 2.1 Section 5.2]. MCP servers
> **MUST** validate that access tokens were issued specifically for them as
> the intended audience, according to [RFC 8707 Section 2]. […] MCP servers
> **MUST** only accept tokens that are valid for use with their own resources.
> MCP servers **MUST NOT** accept or transit any other tokens.

Since the SDK does not do this, **`verify_token` must do it and return `None`
on mismatch.** Without it, any token our AS issued for *any other* resource in
the same tenant would be accepted here — precisely the confused-deputy attack
RFC 8707 exists to prevent. **We would be non-compliant with a `MUST`.**

This is the highest-priority acceptance criterion on the verifier bead, not a
nice-to-have.

### Two spec `SHOULD`s we would also currently miss

1. **`scope` in the 401 challenge.** [§Scope Selection Strategy][spec-authz]
   says servers **SHOULD** include a `scope` parameter in `WWW-Authenticate`.
   Our prototype's 401 (fact 4) emits `error`, `error_description` and
   `resource_metadata` but **no `scope`** —
   `RequireAuthMiddleware._send_auth_error` (`bearer_auth.py:113-120`) has no
   code path for it.
2. **`scope` in the 403.** The SDK emits 403 `insufficient_scope`
   (`bearer_auth.py:106-108`) but again without the `scope=` parameter.

Both need either a custom middleware wrapper or an upstream patch. Neither
blocks a working connector; both are compliance gaps worth tracking.

---

## Open risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Claude.ai's registration behaviour is undocumented.** Unknown whether it does CIMD, DCR, or neither. | Medium | Recommendation does not depend on it. Pre-registration (spec priority 1) always works via the Advanced-settings fields. Resolve by experiment in the first build bead. |
| 2 | **WorkOS CIMD/DCR support is vendor-documented, not observed.** I did not create an account or run the flow. | Medium | Prove end-to-end before building on it. If it disappoints, Stytch and Descope are documented alternatives. |
| 3 | **Free-tier limits may have changed.** Pricing pages move. | Low | Re-check [pricing][workos-pricing] before committing. Data is open and read-only, so a tier breach is an availability problem, not a breach. |
| 4 | **SDK omits `scope` from challenges** (spec `SHOULD`). | Low | Custom middleware or upstream patch. Does not block connectors. |
| 5 | **Single-AS limitation.** `authorization_servers` is hardcoded to `[issuer_url]` (fact 4). | Low | Fine for one AS. Would need an SDK change to federate. |
| 6 | **Vendor lock-in / AS migration.** Moving AS invalidates pre-registered credentials; the spec requires clients to re-register (§Authorization Server Binding). | Low | CIMD client IDs are portable across ASes by design. Few enough users that manual re-registration is tolerable. |
| 7 | **No importable introspection verifier** (fact 5). | Low | Write ~40 lines ourselves, or validate JWTs locally instead. |

---

## Proposed follow-up build beads

Proposed only — per the spike's remit, the orchestrator files these. Ordered
by dependency.

### B1. Prove the WorkOS AuthKit flow end to end (spike/prereq)

**Blocked on a human**: someone must create the WorkOS account; an agent
cannot. Before any code: create the account, enable CIMD and DCR, record the
issuer URL, confirm `client_id_metadata_document_supported: true` and
`registration_endpoint` in the AS metadata, and note the free-tier limits.
Resolves risks 2 and 3.

- [ ] WorkOS account created, tenant configured
- [ ] `/.well-known/oauth-authorization-server` fetched and recorded
- [ ] Free-tier limits recorded
- [ ] Decide JWT-local-validation vs introspection based on the token format issued

### B2. Audience-validating `TokenVerifier`

The core RS work. **Acceptance criterion #1 — non-negotiable: reject any token
whose audience is not our `resource_server_url`, returning `None`.** Cite the
spec `MUST` in the docstring so nobody later "simplifies" it away.

- [ ] Validates signature/expiry against the AS (local JWKS, or introspection per B1)
- [ ] **Rejects tokens not audience-bound to our resource URL** (spec `MUST`)
- [ ] Validates `iss` matches the configured issuer
- [ ] Populates `AccessToken.resource`, `.subject`, `.claims`
- [ ] Tests: wrong audience, wrong issuer, expired, malformed, valid
- [ ] Depends on B1

### B3. Wire OAuth mode into the server and CLI

Config plumbing: select static-bearer vs OAuth mode, accept issuer URL and
required scopes, keep `--public-url` semantics. Preserve the existing
static-token path — it still serves Claude Code and curl.

- [ ] Mutually exclusive mode selection with a clear startup error
- [ ] Reuses `auth_settings()`'s `model_validate` discipline
- [ ] Test asserting the RS route list (guards fact 7's footgun)
- [ ] Comment in `auth.py` recording that registration/revocation options are inert in RS mode
- [ ] Depends on B2

### B4. Connect Claude.ai and ChatGPT; document what actually happened

The experiment that resolves risk 1. Try each client **without**
pre-registered credentials first, then with. Record the observed behaviour —
this is the fact the docs would not give us.

- [ ] ChatGPT connects (expect CIMD or DCR)
- [ ] Claude.ai connects; **record whether blank credentials work**
- [ ] README updated with real setup steps for both
- [ ] Depends on B3

### B5. Emit `scope` in 401/403 challenges (compliance)

Addresses risk 4 and the two `SHOULD`s. Small, independent, non-blocking.

- [ ] `scope` in the 401 `WWW-Authenticate`
- [ ] `scope` in the 403 `insufficient_scope`
- [ ] Consider upstreaming to the Python SDK
- [ ] Depends on B3

### B6. Update `PLAN.md` and `README.md`

Original bead acceptance criterion 1: record the decision and rejected options
in `PLAN.md` under *Protecting the HTTP endpoint*, replacing option 3's
`simple-auth`-based framing. **Also correct the DCR-centric language** so the
deprecated framing is not reintroduced.

- [ ] `PLAN.md` names the chosen AS, the assumed users, and the rejected options
- [ ] DCR-vs-CIMD correction recorded
- [ ] `README.md` reflects OAuth as a real option
- [ ] Depends on B4 (so it documents observed behaviour, not hopes)

---

## Appendix: spike code

`tmp/oauth_spike.py` and `tmp/dcr_check.py` are throwaway and deliberately
**not committed** (`tmp/` is gitignored). Facts 4 and 7 record their full
output, which is the part worth keeping.
