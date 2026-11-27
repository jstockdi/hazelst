"""DB -> `ReportDigest`: the only input `report-write` sees (technical-design §5).

The digest carries the derived numbers (windows, days with `precip_type` and
tags), the NWS discussions (Key Messages + Discussion only), active alerts,
and yesterday's verification. `report_check` checks a draft against the same
digest, so every claim the draft makes is traceable to it.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hazelst.models import Accumulation, ReportPayload

PrecipType = Literal["snow", "ice", "rain", "mix", "dry"]
WindowKey = Literal["1d", "3d", "5d"]
StormType = Literal["clipper", "coastal", "inland_runner", "none"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DigestWindow(_Model):
    snow_in: float
    lo_in: float
    hi_in: float
    confidence: Literal["high", "medium", "low"]
    by_model: dict[str, float | None] = Field(default_factory=dict)
    base_snow_in: float | None = None
    nws_in: float | None = None
    nws_ice_in: float | None = None
    nws_disagrees: bool = False


class DigestDay(_Model):
    date: dt.date
    snow_in: float
    precip_in: float
    precip_type: PrecipType
    hi_f: float | None = None
    lo_f: float | None = None
    gust_mph: float | None = None
    freezing_level_ft: float | None = None
    tags: list[str] = Field(default_factory=list)


class DigestAlert(_Model):
    event: str
    headline: str | None = None
    onset: str | None = None
    expires: str | None = None


class DigestContext(_Model):
    """A non-resort snow value for one past day: never the mountain's own snowfall."""

    date: dt.date
    source: str  # nohrsc | pns | lsr | ghcn
    place: str
    snow_in: float
    elev_ft: int | None = None
    distance_mi: float | None = None


class DigestAccumulation(_Model):
    """Resort-reported snow (`last7_in`: None = no report), and, apart from it, the
    other sources for the same days (`not_resort_reported`, at most NOHRSC plus the
    three highest point reports per day)."""

    source: str
    last7_in: list[float | None] = Field(default_factory=list)
    season_in: float | None = None
    base_in: float | None = None
    not_resort_reported: list[DigestContext] = Field(default_factory=list)


class DigestMountain(_Model):
    key: str
    name: str
    state: str
    summit_ft: int
    base_ft: int
    season_open: dt.date | None = None
    favored_by: list[str] = Field(default_factory=list)
    windows: dict[WindowKey, DigestWindow]
    days: list[DigestDay]
    storm_type: StormType | None = None
    alerts: list[DigestAlert] = Field(default_factory=list)
    accumulation: DigestAccumulation | None = None

    def day(self, when: dt.date) -> DigestDay | None:
        return next((d for d in self.days if d.date == when), None)


class DigestDiscussion(_Model):
    office: str  # BTV | ALY | GYX | WPC
    kind: str = "AFD"  # AFD | WPC
    issued_at: str | None = None
    text: str  # summary sections, whitespace-normalized
    storm_words: list[str] = Field(default_factory=list)  # sources.discussions.STORM_WORDS keys


class DigestVerification(_Model):
    mountain: str
    window: str
    forecast_in: float | None
    observed_in: float | None
    result: str  # HIT | MISS | NO_OBS


class ReportDigest(_Model):
    date: dt.date
    edition: Literal["0700", "0815", "1440"] = "0700"
    issued: str
    days: list[dt.date]
    mountains: list[DigestMountain]
    discussions: list[DigestDiscussion] = Field(default_factory=list)
    stale_models: list[str] = Field(default_factory=list)
    verification: list[DigestVerification] = Field(default_factory=list)

    def mountain(self, key: str) -> DigestMountain | None:
        return next((m for m in self.mountains if m.key == key), None)


# --- build ------------------------------------------------------------------------

CONTEXT_PER_DAY = 3  # point reports per day in the digest, highest elevation first


def digest_accumulation(acc: Accumulation) -> DigestAccumulation:
    """The page's accumulation for the writer, with each day's context kept apart."""
    context: list[DigestContext] = []
    for day in acc.days:
        grid = [c for c in day.context if c.source == "nohrsc"]
        points = sorted(
            (c for c in day.context if c.source != "nohrsc"),
            key=lambda c: (c.elev_ft is None, -(c.elev_ft or 0), c.distance_mi or 0),
        )[:CONTEXT_PER_DAY]
        context += [
            DigestContext(
                date=day.date,
                source=c.source,
                place=c.place,
                snow_in=c.snow_in,
                elev_ft=c.elev_ft,
                distance_mi=c.distance_mi,
            )
            for c in (*grid, *points)
        ]
    return DigestAccumulation(
        source=acc.source,
        last7_in=list(acc.last7_in),
        season_in=acc.season_in,
        base_in=acc.base_in,
        not_resort_reported=context,
    )


def from_payload(
    payload: ReportPayload, verification: Sequence[DigestVerification] = ()
) -> ReportDigest:
    """The digest for one edition, from the page JSON the job already built."""
    mountains = [
        DigestMountain(
            key=m.key,
            name=m.name,
            state=m.state,
            summit_ft=m.summit_ft,
            base_ft=m.base_ft,
            season_open=m.season_open,
            windows={
                key: DigestWindow(
                    snow_in=w.snow_in,
                    lo_in=w.range[0],
                    hi_in=w.range[1],
                    confidence=w.confidence,
                    by_model=dict(w.by_model),
                    base_snow_in=w.base_snow_in,
                    nws_in=w.nws_in,
                    nws_ice_in=w.nws_ice_in,
                    nws_disagrees=w.nws_disagrees,
                )
                for key, w in m.forecast.windows.items()
            },
            days=[
                DigestDay(
                    date=d.date,
                    snow_in=d.snow_in,
                    precip_in=d.precip_in,
                    precip_type=d.precip_type,
                    hi_f=d.hi_f,
                    lo_f=d.lo_f,
                    gust_mph=d.gust_mph,
                    freezing_level_ft=d.freezing_level_ft,
                    tags=list(d.tags),
                )
                for d in m.forecast.days
            ],
            storm_type=m.forecast.storm_type,
            alerts=[DigestAlert(event=e) for e in m.alerts],
            accumulation=digest_accumulation(m.accumulation) if m.accumulation else None,
        )
        for m in payload.mountains
    ]
    days = [d.date for d in payload.mountains[0].forecast.days] if payload.mountains else []
    return ReportDigest(
        date=payload.date,
        edition=payload.edition or "0700",
        issued=payload.issued.isoformat(timespec="seconds"),
        days=days,
        mountains=mountains,
        discussions=[
            DigestDiscussion(
                office=office, text=text, storm_words=payload.storm_words.get(office, [])
            )
            for office, text in payload.discussions.items()
        ],
        stale_models=[r.model for r in payload.models if r.stale],
        verification=list(verification),
    )


def yesterday_verification(conn: sqlite3.Connection, for_date: dt.date) -> list[DigestVerification]:
    """How the previous day's 7:00 raw-mean 1d forecast scored, per mountain."""
    rows = conn.execute(
        "SELECT mountain, window, forecast_in, observed_in, result FROM verification"
        " WHERE for_date = ? AND edition = '0700' AND method = 'raw_mean' AND window = '1d'"
        " ORDER BY mountain",
        ((for_date - dt.timedelta(days=1)).isoformat(),),
    ).fetchall()
    return [
        DigestVerification(
            mountain=r[0], window=r[1], forecast_in=r[2], observed_in=r[3], result=r[4]
        )
        for r in rows
    ]


def build(conn: sqlite3.Connection, payload: ReportPayload) -> ReportDigest:
    return from_payload(payload, yesterday_verification(conn, payload.date))
