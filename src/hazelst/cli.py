"""Command-line entry point: `hazelst <command>`.

The CLI is the admin surface (run over `docker exec` on frank). Commands are
added by the job that implements them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence

from hazelst import __version__, config

Handler = Callable[[argparse.Namespace], int]


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = config.load()
    out = {
        "settings": cfg.settings.public_dict(),
        "timezone": cfg.timezone,
        "mountains": [m.key for m in cfg.mountains]
        if not args.full
        else [m.model_dump(mode="json") for m in cfg.mountains],
    }
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hazelst",
        description="Hazel St Forecast: daily ski forecast service and admin CLI.",
    )
    parser.add_argument("--version", action="version", version=f"hazelst {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_config = sub.add_parser(
        "config", help="print the effective configuration (secrets shown as set/unset)"
    )
    p_config.add_argument("--full", action="store_true", help="include every mountain field")
    p_config.set_defaults(handler=_cmd_config)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except config.ConfigError as exc:
        print(f"hazelst: configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
