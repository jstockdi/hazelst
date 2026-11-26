"""NWS Public Information Statements (PNS): spotter and CoCoRaHS snowfall reports.

Live path: `api.weather.gov/products/types/PNS/locations/{office}` lists recent
products; each product's JSON carries `productText`. Archive path (IEM AFOS):
`api/1/nws/afos/list.json?pil=PNS{office}&date=…` and `api/1/nwstext/{id}`
return the same text. Both are keyed by the AFOS product id
(`202602071335-KBTV-NOUS41-PNSBTV`), so a product is fetched and stored once
whichever path found it.

Only the `**METADATA**` block is parsed; a PNS without one (a transmitter
notice, a climate summary) is stored in `discussion` and yields no reports.
See docs/design/data-sources.md, "NWS PNS snowfall reports".
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from hazelst.config import Mountain
from hazelst.sources import snow_report
from hazelst.sources.base import Fetched, get
from hazelst.sources.snow_report import SnowReportRow

LIST_SOURCE = "nws_pns_list"
SOURCE = "nws_pns"
ARCHIVE_LIST_SOURCE = "iem_afos_list"
ARCHIVE_SOURCE = "iem_nwstext"

OFFICES: tuple[str, ...] = ("BTV", "ALY", "GYX")
LIST_URL = "https://api.weather.gov/products/types/PNS/locations/{office}"
ARCHIVE_LIST_URL = "https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json"
ARCHIVE_TEXT_URL = "https://mesonet.agron.iastate.edu/api/1/nwstext/{product_id}"

# BTV, ALY, and GYX all issue in Eastern time.
OFFICE_TZ = ZoneInfo("America/New_York")
SNOW_TYPES: dict[str, int | None] = {"SNOW_24": 24, "SNOW": None}  # type -> hours
PRODUCT_ID = re.compile(r"^(\d{12})-K([A-Z]{3})-[A-Z0-9]{6}-PNS[A-Z]{3}$")


class NormalizeError(ValueError):
    """A response does not have the expected shape."""


@dataclass(frozen=True)
class ListedProduct:
    """One entry of a product listing, before its text is fetched."""

    product_id: str  # AFOS id
    office: str
    issued_at: str
    url: str
    source: str  # the source that will fetch its text


@dataclass(frozen=True)
class PnsProduct:
    product_id: str
    office: str
    issued_at: str
    text: str
    reports: list[SnowReportRow]


# --- fetch -------------------------------------------------------------------------


def fetch_list(client: httpx.Client, office: str) -> Fetched:
    return get(client, LIST_SOURCE, LIST_URL.format(office=office), meta={"office": office})


def fetch_archive_list(client: httpx.Client, office: str, day: date) -> Fetched:
    return get(
        client,
        ARCHIVE_LIST_SOURCE,
        ARCHIVE_LIST_URL,
        {"pil": f"PNS{office}", "date": day.isoformat()},
        meta={"office": office},
    )


def fetch_product(client: httpx.Client, listed: ListedProduct) -> Fetched:
    return get(
        client,
        listed.source,
        listed.url,
        meta={"product_id": listed.product_id, "office": listed.office},
    )


# --- listings ----------------------------------------------------------------------


def afos_id(issued: datetime, wmo_office: str, wmo_id: str, office: str) -> str:
    """The AFOS product id IEM uses, built from api.weather.gov listing fields."""
    return f"{issued.astimezone(UTC):%Y%m%d%H%M}-{wmo_office}-{wmo_id}-PNS{office}"


def issued_from_id(product_id: str) -> str:
    match = PRODUCT_ID.match(product_id)
    if match is None:
        raise NormalizeError(f"not a PNS product id: {product_id!r}")
    issued = datetime.strptime(match.group(1), "%Y%m%d%H%M").replace(tzinfo=UTC)
    return snow_report.isoformat(issued)


def parse_list(fetched: Fetched) -> list[ListedProduct]:
    """Products in an api.weather.gov or IEM AFOS listing."""
    office = str(fetched.meta["office"])
    data: Any = fetched.json()
    out: list[ListedProduct] = []
    try:
        if fetched.source == LIST_SOURCE:
            for item in data["@graph"]:
                issued = datetime.fromisoformat(item["issuanceTime"])
                pid = afos_id(issued, item["issuingOffice"], item["wmoCollectiveId"], office)
                out.append(
                    ListedProduct(pid, office, snow_report.isoformat(issued), item["@id"], SOURCE)
                )
        elif fetched.source == ARCHIVE_LIST_SOURCE:
            for item in data["data"]:
                pid = item["product_id"]
                out.append(
                    ListedProduct(
                        pid,
                        office,
                        issued_from_id(pid),
                        ARCHIVE_TEXT_URL.format(product_id=pid),
                        ARCHIVE_SOURCE,
                    )
                )
        else:
            raise NormalizeError(f"not a PNS listing: {fetched.source}")
    except (KeyError, TypeError, ValueError) as exc:
        raise NormalizeError(f"{fetched.source} {office}: {exc}") from exc
    return out


# --- normalize ---------------------------------------------------------------------


def _clean(token: str) -> str:
    """Drop `null` placeholders (GYX writes ` null Portland Jetport`) and squeeze spaces."""
    words = [w for w in token.split() if w.lower() != "null"]
    return " ".join(words)


def _amount(raw: str) -> float | None:
    value = raw.strip().upper()
    if value == "T":  # trace
        return 0.0
    try:
        return float(value)
    except ValueError:
        return None


def parse_metadata(
    product_id: str, text: str, mountains: Sequence[Mountain]
) -> list[SnowReportRow]:
    """Snowfall reports from the `**METADATA**` block(s) of a PNS text.

    Each line: `:M/DD/YYYY,HHMM AM, ST, County, Place, , , lat, lon, TYPE, value,
    Inch, Provider, description,`. Fields are located around the TYPE token so
    a comma inside a place name does not shift them.
    """
    rows: list[SnowReportRow] = []
    in_block = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.upper().startswith("**METADATA**"):
            in_block = True
            continue
        if not in_block:
            continue
        if not line.startswith(":"):
            if line.startswith("$$"):
                in_block = False
            continue
        fields = [f.strip() for f in line[1:].split(",")]
        kind_at = next((i for i, f in enumerate(fields) if f in SNOW_TYPES), None)
        if kind_at is None or kind_at < 6 or len(fields) < kind_at + 3:
            continue  # rain, ice, snow depth, or a malformed line
        if fields[kind_at + 2].lower() not in {"inch", "inches"}:
            continue
        amount = _amount(fields[kind_at + 1])
        try:
            lat, lon = float(fields[kind_at - 2]), float(fields[kind_at - 1])
            local = datetime.strptime(f"{fields[0]} {fields[1]}", "%m/%d/%Y %I%M %p")
        except ValueError:
            continue
        if amount is None:
            continue
        place = _clean(", ".join(f for f in fields[4 : kind_at - 2] if f))
        state = fields[2]
        rows.append(
            snow_report.make_row(
                product_id=product_id,
                place=f"{place}, {state}" if state else place,
                lat=lat,
                lon=lon,
                ending_at=local.replace(tzinfo=OFFICE_TZ),
                snow_in=amount,
                hours=SNOW_TYPES[fields[kind_at]],
                mountains=mountains,
            )
        )
    return rows


def normalize(fetched: Fetched, mountains: Sequence[Mountain]) -> PnsProduct:
    """A fetched product (api.weather.gov JSON or IEM text) → its text and reports."""
    office = str(fetched.meta.get("office", ""))
    if fetched.source == SOURCE:
        data: Any = fetched.json()
        try:
            text = str(data["productText"])
            issued = datetime.fromisoformat(data["issuanceTime"])
            pid = afos_id(issued, data["issuingOffice"], data["wmoCollectiveId"], office)
        except (KeyError, TypeError, ValueError) as exc:
            raise NormalizeError(f"{fetched.url}: {exc}") from exc
        issued_at = snow_report.isoformat(issued)
    elif fetched.source == ARCHIVE_SOURCE:
        pid = str(fetched.meta["product_id"])
        text = fetched.body.decode("utf-8", errors="replace")
        issued_at = issued_from_id(pid)
        office = office or pid[-3:]
    else:
        raise NormalizeError(f"not a PNS product: {fetched.source}")
    return PnsProduct(pid, office, issued_at, text, parse_metadata(pid, text, mountains))


# --- store -------------------------------------------------------------------------


def known_ids(conn: sqlite3.Connection) -> set[str]:
    """AFOS ids of PNS products already stored (skipped on the next fetch)."""
    rows = conn.execute("SELECT product_id FROM discussion WHERE kind = 'PNS'").fetchall()
    return {r[0] for r in rows}


def store(conn: sqlite3.Connection, product: PnsProduct) -> int:
    """Store the text in `discussion` and the reports in `snow_report`; returns reports written."""
    conn.execute(
        "INSERT OR IGNORE INTO discussion (product_id, office, kind, issued_at, text)"
        " VALUES (?, ?, 'PNS', ?, ?)",
        (product.product_id, product.office, product.issued_at, product.text),
    )
    return snow_report.store(conn, product.reports, "pns")
