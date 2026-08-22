"""Static bearer-token authorization for the HTTP transport.

This is the cheapest thing that keeps casual traffic off a publicly exposed
Funnel URL: one shared secret, compared in constant time, no authorization
server and no token lifecycle. The upstream Council Gateway data is open and
read-only, so what this protects is bandwidth and abuse, not the data.

**It does not work with the browser clients.** Claude.ai custom connectors and
ChatGPT speak OAuth or no-auth; neither can be told to send a fixed
``Authorization`` header. So this covers Claude Code, ``curl`` and scripted
clients only. Full OAuth is tracked separately.

Authorization applies to HTTP transports only — stdio bypasses it entirely,
because there is no request to carry a header on.
"""

from __future__ import annotations

import secrets

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

#: Reported as the token's ``client_id``. A shared secret says nothing about
#: who presented it, so every caller is the same anonymous principal.
STATIC_CLIENT_ID = "static"


class StaticTokenVerifier(TokenVerifier):
    """Accept exactly one pre-shared bearer token."""

    def __init__(self, token: str) -> None:
        self._expected = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an ``AccessToken`` if ``token`` is the configured secret.

        ``secrets.compare_digest`` rather than ``==`` so that a wrong token
        costs the same time to reject however many leading characters it got
        right, which denies an attacker a byte-at-a-time oracle.

        The comparison is on UTF-8 *bytes*, not ``str``: ``compare_digest``
        raises ``TypeError`` on strings holding non-ASCII characters, and
        header values reach us Latin-1-decoded, so a caller can put one there.
        Left as ``str`` that would surface as a 500 instead of a 401.
        """
        if not secrets.compare_digest(token.encode(), self._expected):
            return None
        return AccessToken(token=token, client_id=STATIC_CLIENT_ID, scopes=[])


def auth_settings(public_url: str) -> AuthSettings:
    """Auth settings advertising this server at ``public_url``.

    ``public_url`` is the externally reachable origin — the
    ``https://….ts.net`` address when behind a Tailscale Funnel, not the local
    bind address — since it ends up in the RFC 9728 metadata that clients read.

    ``issuer_url`` is a **placeholder**: there is no authorization server in
    this scheme, and nothing is issuing or rotating tokens. The SDK requires
    the field, and it is echoed as ``authorization_servers`` in the metadata
    served at ``/.well-known/oauth-protected-resource/mcp``, where an OAuth
    client would follow it and find nothing. Do not read its presence as a
    sign that this is a real OAuth deployment.

    Raises ``pydantic.ValidationError`` if ``public_url`` is not a valid HTTP
    URL, so a typo fails at startup rather than at the first request.

    Built via ``model_validate`` rather than ``AuthSettings(...)`` so the URLs
    are coerced *by the model*. That matters: ``AuthSettings`` sets
    ``url_preserve_empty_path=True`` to stop a path-less origin gaining a
    trailing slash, and calling ``AnyHttpUrl(...)`` ourselves would sidestep
    that config and turn ``http://host:8000`` into ``http://host:8000/``.
    RFC 8414/9207 compare issuers by exact string, so the stray slash would be
    a real mismatch, not cosmetic.
    """
    return AuthSettings.model_validate(
        {
            "issuer_url": public_url,
            "resource_server_url": f"{public_url}/mcp",
            "required_scopes": None,
        }
    )
