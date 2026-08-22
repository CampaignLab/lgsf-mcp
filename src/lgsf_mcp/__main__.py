"""Command-line entry point for the lgsf-mcp server."""

from __future__ import annotations

import argparse
import os
import sys

from .server import DEFAULT_HOST, DEFAULT_PORT, create_server

#: Shared bearer token required on every HTTP request when set. Taken from the
#: environment rather than a flag so it stays out of ``ps`` output and shell
#: history.
TOKEN_ENV_VAR = "LGSF_MCP_TOKEN"

STDIO_TOKEN_WARNING = (
    f"warning: {TOKEN_ENV_VAR} is set but --transport stdio has no requests to "
    "authenticate; ignoring it. Use --transport streamable-http to enforce it."
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lgsf-mcp",
        description="MCP server over the Council Gateway API "
        "(UK local government data).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="Transport to serve on (default: %(default)s).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Bind address for --transport streamable-http (default: %(default)s).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Bind port for --transport streamable-http (default: %(default)s).",
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help="Externally reachable origin advertised to clients, without a "
        "trailing slash (default: http://HOST:PORT). Set this to the "
        "https://….ts.net URL when serving behind a Tailscale Funnel, so that "
        f"the metadata points somewhere reachable. Only used when {TOKEN_ENV_VAR} "
        "is set.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    token = os.environ.get(TOKEN_ENV_VAR) or None

    if args.transport == "stdio":
        # Authorization is an HTTP-transport concept; silently dropping the
        # token would look like it was being enforced.
        if token is not None:
            print(STDIO_TOKEN_WARNING, file=sys.stderr)
        create_server().run(transport="stdio")
        return

    public_url = args.public_url or f"http://{args.host}:{args.port}"
    server = create_server(token=token, public_url=public_url)
    server.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
