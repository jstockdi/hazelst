"""DB -> `ReportDigest`: the only input `report-write` sees (technical-design §5).

The digest carries the derived numbers (windows, days with `precip_type` and
tags), the NWS discussions (Key Messages + Discussion only), active alerts,
and yesterday's verification. `report_check` checks a draft against the same
digest, so every claim the draft makes is traceable to it.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class DigestAccumulation(_Model):
    source: str
    last7_in: list[float | None] = Field(default_factory=list)
    season_in: float | None = None
    base_in: float | None = None


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
    kind: str  # AFD | WPC
    issued_at: str
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
