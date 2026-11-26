"""NWS Area Forecast Discussions (AFD) for BTV, ALY, and GYX.

Each ingest fetches an office's AFD list and its latest product. The stored
`discussion.text` is the full product text; `summary()` keeps the
forecaster's sections and `storm_words()` matches track and pattern keywords.
Both normalize whitespace first: product text wraps lines mid-phrase, so
"travel northward through the\\neastern" must still match.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from typing import NamedTuple

import httpx

from hazelst.sources.base import Fetched, get

LIST_SOURCE = "nws_afd_list"
PRODUCT_SOURCE = "nws_afd"
API = "https://api.weather.gov"
OFFICES: tuple[str, ...] = ("BTV", "ALY", "GYX")

SUMMARY_SECTIONS = (
    ".WHAT HAS CHANGED",
    ".KEY MESSAGES",
    ".SYNOPSIS",
    ".DISCUSSION",
    ".NEAR TERM",
    ".SHORT TERM",
    ".LONG TERM",
)
SECTION_START = re.compile(r"\n(?=\.[A-Z][^\n]*?\.\.\.)")

# Keyword -> pattern, matched case-insensitively on whitespace-normalized text.
# The first three are tracks (storm_type); the rest are pattern hints.
STORM_WORDS: tuple[tuple[str, str], ...] = (
    ("clipper", r"\bclipper\b"),
    ("coastal", r"nor'?easter|coastal (?:low|storm)|offshore low|benchmark"),
    (
        "inland_runner",
        r"inland runner|lakes cutter|track(?:s|ing)? (?:west|through the eastern)"
        r"|travel northward through the eastern",
    ),
    ("upslope", r"upslope|northwest flow|nw flow|terrain-enhanced"),
    ("freezing_rain", r"freezing rain|\bice accumulation|glaze"),
    ("low_level_jet", r"low[- ]level jet"),
)
TRACKS = ("clipper", "coastal", "inland_runner")


class NormalizeError(ValueError):
    """A response does not have the expected shape."""


class DiscussionRow(NamedTuple):
    product_id: str
    office: str
    kind: str
    issued_at: str
    text: str


# --- fetch --------------------------------------------------------------------------


def list_url(office: str, kind: str = "AFD") -> str:
    return f"{API}/products/types/{kind}/locations/{office}"


def latest_product_id(fetched: Fetched) -> str:
    try:
        return str(fetched.json()["@graph"][0]["id"])
    except (KeyError, IndexError, TypeError) as exc:
        raise NormalizeError(f"product list {fetched.url}: {exc}") from exc


def fetch(client: httpx.Client, offices: Sequence[str] = OFFICES) -> list[Fetched]:
    """For each office, the AFD list and the latest AFD product."""
    out: list[Fetched] = []
    for office in offices:
        listing = get(client, LIST_SOURCE, list_url(office), meta={"office": office})
        out.append(listing)
        if listing.http_status != 200:
            continue
        product_id = latest_product_id(listing)
        out.append(
            get(
                client,
                PRODUCT_SOURCE,
                f"{API}/products/{product_id}",
                meta={"office": office, "kind": "AFD"},
            )
        )
    return out


def label(fetched: Fetched) -> str:
    kind = "afd-list" if fetched.source == LIST_SOURCE else "afd"
    return f"{kind}-{fetched.meta['office']}"


# --- normalize ----------------------------------------------------------------------


def normalize(fetched: Fetched) -> DiscussionRow:
    """A `discussion` row from one product response."""
    try:
        data = fetched.json()
        office = str(data.get("issuingOffice") or fetched.meta["office"])
        return DiscussionRow(
            product_id=str(data["id"]),
            office=office[1:] if len(office) == 4 and office.startswith("K") else office,
            kind=str(data.get("productCode") or fetched.meta.get("kind", "AFD")),
            issued_at=str(data["issuanceTime"]),
            text=str(data["productText"]),
        )
    except (KeyError, TypeError) as exc:
        raise NormalizeError(f"product {fetched.url}: {exc}") from exc


def normalize_whitespace(text: str) -> str:
    """Join wrapped lines: single spaces inside paragraphs, one blank line between them."""
    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return "\n\n".join(" ".join(p.split()) for p in paragraphs if p.strip())


def summary(text: str) -> str:
    """The forecaster's summary sections (Key Messages, Discussion, …), whitespace-normalized.

    Aviation, marine, hydrology, and watches/warnings sections are dropped; text
    with none of the summary sections is returned whole.
    """
    keep = [
        section.strip()
        for section in SECTION_START.split(text)
        if section.strip().split("...")[0] in SUMMARY_SECTIONS
    ]
    body = "\n\n".join(keep) or text
    return normalize_whitespace(body.replace("&&", ""))


def storm_words(text: str) -> list[str]:
    """Keywords from `STORM_WORDS` found in `text`, in table order."""
    flat = " ".join(text.split())
    return [name for name, pattern in STORM_WORDS if re.search(pattern, flat, re.IGNORECASE)]


# --- store --------------------------------------------------------------------------


def store(conn: sqlite3.Connection, rows: Iterable[DiscussionRow]) -> int:
    """Insert discussions; a product already stored is left as is."""
    params = [tuple(r) for r in rows]
    conn.executemany(
        "INSERT OR IGNORE INTO discussion (product_id, office, kind, issued_at, text)"
        " VALUES (?, ?, ?, ?, ?)",
        params,
    )
    return len(params)
