"""Iowa Environmental Mesonet Local Storm Reports (LSR), as GeoJSON.

`mesonet.agron.iastate.edu/geojson/lsr.php?wfos=BTV,ALY,GYX&sts=…&ets=…` (UTC
`YYYYMMDDHHMM`). Snow reports have `type == "S"`; `magf` is inches and `valid`
is the report time in UTC. LSRs do not say how many hours a report covers, so
`snow_report.hours` is null. The PNS text archive on the same host is in
`pns.py`. See docs/design/data-sources.md, "IEM Local Storm Reports".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from hazelst.config import Mountain
from hazelst.sources import snow_report
from hazelst.sources.base import Fetched, get
from hazelst.sources.snow_report import SnowReportRow

LSR_SOURCE = "iem_lsr"
LSR_URL = "https://mesonet.agron.iastate.edu/geojson/lsr.php"
WFOS: tuple[str, ...] = ("BTV", "ALY", "GYX")


class NormalizeError(ValueError):
    """A response does not have the expected shape."""


def lsr_params(start: datetime, end: datetime, wfos: Sequence[str] = WFOS) -> dict[str, str]:
    return {
        "wfos": ",".join(wfos),
        "sts": f"{start.astimezone(UTC):%Y%m%d%H%M}",
        "ets": f"{end.astimezone(UTC):%Y%m%d%H%M}",
    }


def fetch_lsr(client: httpx.Client, start: datetime, end: datetime) -> Fetched:
    return get(client, LSR_SOURCE, LSR_URL, lsr_params(start, end))


def _place(props: dict[str, Any]) -> str:
    city = str(props.get("city") or "").strip()
    state = str(props.get("st") or props.get("state") or "").strip()
    return f"{city}, {state}" if state else city


def normalize_lsr(fetched: Fetched, mountains: Sequence[Mountain]) -> list[SnowReportRow]:
    """Snow reports in an LSR GeoJSON collection; other report types are skipped."""
    data: Any = fetched.json()
    try:
        features = data["features"]
    except (KeyError, TypeError) as exc:
        raise NormalizeError(f"{fetched.url}: no features ({exc})") from exc
    rows: list[SnowReportRow] = []
    for feature in features:
        props = feature.get("properties") or {}
        if props.get("type") != "S" or str(props.get("unit", "")).lower() != "inch":
            continue
        magf, lat, lon, valid = (
            props.get("magf"),
            props.get("lat"),
            props.get("lon"),
            props.get("valid"),
        )
        if magf is None or lat is None or lon is None or not valid or float(magf) < 0:
            continue
        rows.append(
            snow_report.make_row(
                product_id=str(props.get("product_id") or ""),
                place=_place(props),
                lat=float(lat),
                lon=float(lon),
                ending_at=datetime.fromisoformat(str(valid).replace("Z", "+00:00")),
                snow_in=float(magf),
                hours=None,
                mountains=mountains,
            )
        )
    return rows
