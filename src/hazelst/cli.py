"""Command-line entry point: `hazelst <command>`.

The CLI is the admin surface (run over `docker exec` on frank). Commands are
added by the job that implements them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from hazelst import __version__, config, db, jobs
from hazelst.sources import base

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


def _cmd_migrate(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = db.connect(cfg.settings.db_path)
    try:
        before = db.user_version(conn)
        applied = db.migrate(conn)
        synced = db.sync_mountains(conn, cfg.mountains)
        after = db.user_version(conn)
    finally:
        conn.close()
    print(
        json.dumps(
            {
                "db": str(cfg.settings.db_path),
                "user_version": {"before": before, "after": after},
                "applied": applied,
                "mountains": synced,
            }
        )
    )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from hazelst import web

    cfg = config.load()
    conn = db.connect(cfg.settings.db_path)
    try:
        db.migrate(conn)
        db.sync_mountains(conn, cfg.mountains)
    finally:
        conn.close()
    app = web.create_app(cfg.settings.db_path)
    uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=False)
    return 0


def _cmd_healthcheck(args: argparse.Namespace) -> int:
    """Exit 0 when GET /health returns 200 (the image HEALTHCHECK; no curl in the image)."""
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{args.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=args.timeout) as resp:
            ok = resp.status == 200
    except (urllib.error.URLError, OSError) as exc:
        print(f"hazelst: healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 0 if ok else 1


def _cmd_record(args: argparse.Namespace) -> int:
    cfg = config.load()
    fetch, label = jobs.RECORDERS[args.source]
    out_dir: Path = args.out or (
        config.PROJECT_ROOT / "tests" / "fixtures" / args.source.replace("-", "_")
    )
    out_dir = out_dir / (args.name or jobs.today(cfg).isoformat())
    written = []
    with base.make_client(cfg.settings.nws_user_agent) as client:
        for fetched in fetch(client, cfg.mountains):
            path = out_dir / f"{label(fetched)}.json.gz"
            base.write_fixture(path, fetched)
            written.append({"path": str(path), "http_status": fetched.http_status})
    print(json.dumps({"source": args.source, "fixtures": written}, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load()
    summary = jobs.run_job(cfg, args.job, args.date)
    print(json.dumps(summary, indent=2, default=str))
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

    p_migrate = sub.add_parser(
        "migrate", help="apply pending database migrations and sync the mountain roster"
    )
    p_migrate.set_defaults(handler=_cmd_migrate)

    p_serve = sub.add_parser("serve", help="migrate, then serve the web app")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.set_defaults(handler=_cmd_serve)

    p_health = sub.add_parser("healthcheck", help="exit 0 if the local /health returns 200")
    p_health.add_argument("--port", type=int, default=8080)
    p_health.add_argument("--timeout", type=float, default=3.0)
    p_health.set_defaults(handler=_cmd_healthcheck)
    p_record = sub.add_parser(
        "record", help="fetch a source live and save its raw responses as test fixtures"
    )
    p_record.add_argument("source", choices=sorted(jobs.RECORDERS))
    p_record.add_argument(
        "--out", type=Path, help="fixture folder (default tests/fixtures/<source>)"
    )
    p_record.add_argument("--name", help="subfolder name (default: today's date)")
    p_record.set_defaults(handler=_cmd_record)

    p_run = sub.add_parser("run", help="run one job now (takes the job lock, opens a run row)")
    p_run.add_argument("job", choices=sorted(jobs.JOBS))
    p_run.add_argument(
        "--date", type=date.fromisoformat, help="report date (default: today, America/New_York)"
    )
    p_run.set_defaults(handler=_cmd_run)

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
