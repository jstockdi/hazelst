"""Report payloads built from recorded fixtures, shared by render and web tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from hazelst import config, derive
from hazelst.models import Narrative, ReportPayload
from hazelst.render import page
from hazelst.render.payload import build_payload
from hazelst.sources import discussions, nws, open_meteo
from tests.support import FIXTURES, load_fixtures

MOUNTAINS = config.load_mountains(config.DEFAULT_MOUNTAINS)
PROTOTYPE = config.PROJECT_ROOT / "prototype" / "out"


def sample_payload() -> ReportPayload:
    """2026-11-25: the sample run's model runs (Single Runs API) and its AFD texts."""
    rows = [
        r
        for f in load_fixtures(FIXTURES / "open_meteo" / "2026-11-25")
        for r in open_meteo.normalize(f, {})
    ]
    sample = json.loads((PROTOTYPE / "2026-11-25.json").read_text())
    texts = {d["office"]: d["text"] for d in sample["discussions"]}
    as_of = datetime(2026, 11, 25, 15, 44, tzinfo=UTC)
    forecast = derive.derive(
        MOUNTAINS, derive.Inputs(rows, [], texts), for_date=date(2026, 11, 25), as_of=as_of
    )
    return build_payload(
        forecast, MOUNTAINS, issued=as_of, status="published", discussion_texts=texts
    )


def sample_narrative() -> Narrative:
    return page.parse_narrative((PROTOTYPE / "2026-11-25.md").read_text())


def recorded_payload() -> ReportPayload:
    """2026-11-26 7:00 AM edition, numbers only, from that morning's recorded fixtures."""
    om = load_fixtures(FIXTURES / "open_meteo" / "2026-11-26")
    runs = open_meteo.run_times(f for f in om if f.source == open_meteo.META_SOURCE)
    rows = [r for f in om if f.source == open_meteo.SOURCE for r in open_meteo.normalize(f, runs)]
    grid = [
        r
        for f in load_fixtures(FIXTURES / "nws" / "2026-11-26")
        if f.source == nws.GRID_SOURCE
        for r in nws.normalize_grid(f)
    ]
    alerts: dict[str, list[str]] = {}
    for f in load_fixtures(FIXTURES / "nws" / "2026-11-26"):
        if f.source == nws.ALERTS_SOURCE:
            for a in nws.normalize_alerts(f):
                alerts.setdefault(a.mountain, []).append(a.event)
    texts = {
        d.office: d.text
        for f in load_fixtures(FIXTURES / "discussions" / "2026-11-26")
        if f.source == discussions.PRODUCT_SOURCE
        for d in [discussions.normalize(f)]
    }
    as_of = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    forecast = derive.derive(
        MOUNTAINS, derive.Inputs(rows, grid, texts), for_date=date(2026, 11, 26), as_of=as_of
    )
    return build_payload(
        forecast,
        MOUNTAINS,
        issued=as_of,
        status="numbers_only",
        edition="0700",
        alerts=alerts,
        discussion_texts=texts,
    )
