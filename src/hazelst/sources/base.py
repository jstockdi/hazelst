"""Shared source plumbing: the HTTP client, raw `fetch` rows, and recorded fixtures.

A source's `fetch()` returns `Fetched` values (one per HTTP response) without
touching the database; `store_fetch()` writes them as `fetch` rows. Bodies are
gzip-compressed and stored once per SHA-256: a later identical body keeps its
hash but no copy, and `load_body()` finds the stored copy.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from hazelst import __version__
from hazelst.config import DEFAULT_USER_AGENT

DEFAULT_TIMEOUT_S = 40.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class SourceError(RuntimeError):
    """A source returned no usable response after its retries."""


@dataclass(frozen=True)
class Fetched:
    """One raw HTTP response, as stored in a `fetch` row."""

    source: str
    url: str
    params: dict[str, Any]
    http_status: int
    fetched_at: str
    ms: int
    body: bytes
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def json(self) -> Any:
        return json.loads(self.body)


def make_client(
    user_agent: str = DEFAULT_USER_AGENT, timeout_s: float = DEFAULT_TIMEOUT_S
) -> httpx.Client:
    """An HTTP/1.1 client with the identifying `User-Agent` and a per-request timeout."""
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"},
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=True,
    )


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def get(
    client: httpx.Client,
    source: str,
    url: str,
    params: Mapping[str, Any] | None = None,
    *,
    meta: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    retries: int = 2,
    backoff_s: float = 2.0,
) -> Fetched:
    """GET with retries on timeouts, connection errors, 429, and 5xx.

    Any other status is returned as is (the caller decides whether a 404 is
    an error); raises `SourceError` once the retries are spent.
    """
    query = dict(params or {})
    last_error = "no attempt"
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(backoff_s * attempt)
        started = time.monotonic()
        fetched_at = utc_now()
        try:
            response = client.get(url, params=query, headers=dict(headers or {}))
        except httpx.TransportError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        ms = round((time.monotonic() - started) * 1000)
        if response.status_code in RETRY_STATUSES and attempt < retries:
            last_error = f"HTTP {response.status_code}"
            continue
        return Fetched(
            source=source,
            url=url,
            params=query,
            http_status=response.status_code,
            fetched_at=fetched_at,
            ms=ms,
            body=response.content,
            meta=dict(meta or {}),
        )
    raise SourceError(f"{source}: GET {url} failed after {retries + 1} attempts: {last_error}")


# --- fetch rows ---------------------------------------------------------------


def stored_params(fetched: Fetched) -> str:
    """The `fetch.params` JSON: the query sent plus the source's own context."""
    return json.dumps({"query": fetched.params, "meta": fetched.meta}, sort_keys=True)


def store_fetch(conn: sqlite3.Connection, run_id: int | None, fetched: Fetched) -> int:
    """Insert one `fetch` row; the gzip body is stored only if its SHA is new."""
    sha = fetched.sha256
    seen = conn.execute(
        "SELECT 1 FROM fetch WHERE body_sha256 = ? AND body IS NOT NULL LIMIT 1", (sha,)
    ).fetchone()
    body = None if seen else gzip.compress(fetched.body, mtime=0)
    cur = conn.execute(
        "INSERT INTO fetch (run_id, source, url, params, http_status, fetched_at, ms,"
        " body_sha256, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            fetched.source,
            fetched.url,
            stored_params(fetched),
            fetched.http_status,
            fetched.fetched_at,
            fetched.ms,
            sha,
            body,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def load_body(conn: sqlite3.Connection, sha256: str) -> bytes:
    row = conn.execute(
        "SELECT body FROM fetch WHERE body_sha256 = ? AND body IS NOT NULL LIMIT 1", (sha256,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no stored body for sha256 {sha256}")
    return gzip.decompress(row[0])


def load_fetch(conn: sqlite3.Connection, fetch_id: int) -> Fetched:
    """Rebuild a `Fetched` from its `fetch` row (the input to `normalize`)."""
    row = conn.execute(
        "SELECT source, url, params, http_status, fetched_at, ms, body_sha256 FROM fetch"
        " WHERE id = ?",
        (fetch_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no fetch row {fetch_id}")
    params = json.loads(row["params"] or "{}")
    return Fetched(
        source=row["source"],
        url=row["url"],
        params=params.get("query", {}),
        http_status=row["http_status"],
        fetched_at=row["fetched_at"],
        ms=row["ms"] or 0,
        body=load_body(conn, row["body_sha256"]),
        meta=params.get("meta", {}),
    )


# --- recorded fixtures -----------------------------------------------------------


def write_fixture(path: Path, fetched: Fetched) -> None:
    """Write a response as `<name>.json.gz`: the `fetch` row fields and the body text."""
    envelope = {
        "source": fetched.source,
        "url": fetched.url,
        "params": fetched.params,
        "meta": fetched.meta,
        "http_status": fetched.http_status,
        "fetched_at": fetched.fetched_at,
        "ms": fetched.ms,
        "recorded_by": f"hazelst {__version__}",
        "body": fetched.body.decode("utf-8"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(envelope).encode("utf-8"), mtime=0))


def read_fixture(path: Path) -> Fetched:
    envelope = json.loads(gzip.decompress(path.read_bytes()))
    return Fetched(
        source=envelope["source"],
        url=envelope["url"],
        params=envelope["params"],
        http_status=envelope["http_status"],
        fetched_at=envelope["fetched_at"],
        ms=envelope.get("ms", 0),
        body=envelope["body"].encode("utf-8"),
        meta=envelope.get("meta", {}),
    )
