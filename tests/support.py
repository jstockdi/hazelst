"""Test helpers: recorded fixtures and an HTTP transport that replays them."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

import httpx

from hazelst.sources import base

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixtures(folder: Path) -> list[base.Fetched]:
    return [base.read_fixture(p) for p in sorted(folder.glob("*.json.gz"))]


def _key(url: str, params: Mapping[str, object]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return url, tuple(sorted((k, str(v)) for k, v in params.items()))


def replay_transport(recorded: Iterable[base.Fetched]) -> httpx.MockTransport:
    """Serve each recorded body for the same URL and query; 404 for anything else."""
    table = {_key(f.url, f.params): f for f in recorded}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        params = dict(request.url.params.multi_items())
        hit = table.get(_key(url, params))
        if hit is None:
            return httpx.Response(404, text=f"no fixture for {request.url}")
        return httpx.Response(hit.http_status, content=hit.body)

    return httpx.MockTransport(handler)


def replay_client(recorded: Iterable[base.Fetched]) -> httpx.Client:
    return httpx.Client(transport=replay_transport(recorded))
