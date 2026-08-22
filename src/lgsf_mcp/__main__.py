"""Command-line entry point for the lgsf-mcp server."""

from __future__ import annotations

import argparse

from .server import create_server


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
        default="127.0.0.1",
        help="Bind address for --transport streamable-http (default: %(default)s).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for --transport streamable-http (default: %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    server = create_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
