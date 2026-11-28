"""Pydantic models for the forecast numbers (forecast-method.md, "JSON shape")."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, model_validator

WindowKey = Literal["1d", "3d", "5d"]
Confidence = Literal["high", "medium", "low"]
PrecipType = Literal["snow", "ice", "rain", "mix", "dry"]
Elevation = Literal["summit", "base"]
StormType = Literal["clipper", "coastal", "inland_runner", "none"]


# Validation context for reading stored payloads: fields a newer release added
# are dropped instead of rejected, so a rolled-back image still renders them.
READ_STORED = {"ignore_unknown_fields": True}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_unknown_when_reading(cls, data: Any, info: ValidationInfo) -> Any:
        if isinstance(data, dict) and (info.context or {}).get("ignore_unknown_fields"):
            return {k: v for k, v in data.items() if k in cls.model_fields}
        return data


class ForecastWindow(_Model):
    """New snow expected from 6 AM over 24 / 72 / 120 h, summit elevation."""

    snow_in: float
    range: tuple[float, float]
    confidence: Confidence
    by_model: dict[str, float | None]
    base_snow_in: float
    nws_in: float | None
    nws_ice_in: float | None
    nws_disagrees: bool
    # The `corrected` method's value, set only where `method_gate` chose it (method.py).
    corrected_in: float | None = None
    # The model mean, set when the headline comes from `resort_ratio` (ratio.py gate).
    raw_mean_in: float | None = None


class ForecastDay(_Model):
    """One 6 AM -> 6 AM day at one mountain (summit unless named)."""

    date: date
    label: str
    snow_in: float
    precip_in: float
    precip_type: PrecipType
    hi_f: int | None
    lo_f: int | None
    gust_mph: int | None
    freezing_level_ft: int | None
    tags: list[str]
    # The same day at the base, and the elevations with freezing rain (tags.day_precip).
    # None / empty on payloads stored before J33.
    base_precip_type: PrecipType | None = None
    freezing_rain_at: list[Elevation] = []
    # Each model's summit snowfall for the day as Open-Meteo reports it (global
    # and finer models, before any ratio), where the run covers >= 23 hours of
    # the day; the page's model drawer (J35). Empty on payloads stored before J35.
    by_model: dict[str, float] = {}


ForecastMethod = Literal["raw_mean", "resort_ratio"]


class MountainForecast(_Model):
    key: str
    windows: dict[WindowKey, ForecastWindow]
    days: list[ForecastDay]
    storm_type: StormType
    # The method behind snow_in, range, by_model, and the days' snow (`method_gate`, method.py).
    method: ForecastMethod = "raw_mean"
    # A finer-model candidate behind day 1 and the 1d window (fine.py), when chosen;
    # days 2-5 stay on `method`.
    day1_method: str | None = None


class ModelRun(_Model):
    """A model's run as used by one derive: its age at `as_of` and whether it was used."""

    model: str
    issued_at: datetime | None
    age_h: float | None
    stale: bool


class Forecast(_Model):
    """Everything `derive` produces for one report date."""

    date: date
    anchor: datetime
    as_of: datetime
    ingest_run_id: int | None
    models: list[ModelRun]
    used_models: list[str]
    storm_words: dict[str, list[str]]
    mountains: list[MountainForecast]

    def mountain(self, key: str) -> MountainForecast:
        for m in self.mountains:
            if m.key == key:
                return m
        raise KeyError(key)


# --- report payload (stored in report.payload, served at /api/report/<date>.json) ---------

# Every report row is a checked narrative (J33). Rows written before J33 may say
# `numbers_only`; they are kept for history and never shown.
ReportStatus = Literal["published"]


class BestBet(_Model):
    mountain: str
    day: str
    why: str


class ContextReport(_Model):
    """A snow value for a mountain-day that the resort did not report (decisions row 24).

    NOHRSC at the mountain's pixel, or a PNS / LSR / GHCN report within 20 mi.
    Shown and stored beside the resort's number, never in place of it.
    """

    source: str  # nohrsc | pns | lsr | ghcn
    place: str
    snow_in: float
    elev_ft: int | None = None  # report elevation; NOHRSC: the pixel center's
    distance_mi: float | None = None  # from the mountain's point; None for NOHRSC
    hours: int | None = None  # 24, or None for an unknown span (LSR, storm total)
    ending_at: str | None = None  # UTC ISO time the report's span ends


class AccumulationDay(_Model):
    """One 6 AM -> 6 AM day: the resort-reported value, and the other sources beside it."""

    date: date
    snow_in: float | None = None  # resort-reported; None = no report
    source: str | None = None  # resort | resort_llm | snocountry
    context: list[ContextReport] = []


class Accumulation(_Model):
    """Snow that has fallen, as the resort reports it, chosen by `verify.accumulation`.

    `last7_in` holds the resort-reported value for each of the 7 days before
    the report date (6 AM to 6 AM, oldest first; None = no report); `days`
    holds the same values with the non-resort context for each day. `source`
    is where the season total and base came from, and `sources` every
    resort-reported source used.
    """

    source: str
    last7_in: list[float | None]
    season_in: float | None = None
    base_in: float | None = None
    sources: list[str] = []
    days: list[AccumulationDay] = []

    @property
    def has_resort_data(self) -> bool:
        """True when the resort reported anything: a season total, base, or a day's snow."""
        return (
            self.season_in is not None
            or self.base_in is not None
            or any(v is not None for v in self.last7_in)
        )


class VerificationSummary(_Model):
    """Result counts for the model mean's 1d forecast, per edition, for one date."""

    for_date: date
    editions: dict[str, dict[str, int]]  # edition -> {HIT | MISS | NO_OBS: count}


class MountainReport(_Model):
    key: str
    name: str
    state: str
    summit_ft: int
    base_ft: int
    season_open: date | None
    is_open: bool
    forecast: MountainForecast
    accumulation: Accumulation | None = None
    alerts: list[str] = []
    powder_score: int | None = None


class Narrative(_Model):
    """The written report: front matter plus markdown sections (narrative.md)."""

    headline: str
    best_bet: BestBet | None = None
    body_md: str = ""


class ReportPayload(_Model):
    """The page JSON for one date and edition."""

    date: date
    issued: datetime
    edition: str | None = None  # 0700 | 0815 | 1440
    # Always "published"; kept so an image from before J33 can still read new rows.
    status: ReportStatus = "published"
    anchor: datetime
    days: list[str]  # day labels, 6 AM -> 6 AM, "Wed 25"
    past_days: list[str]  # the 7 days before the anchor, oldest first
    models: list[ModelRun]
    used_models: list[str]
    discussions: dict[str, str] = {}  # office -> summary text
    storm_words: dict[str, list[str]] = {}
    sources_unavailable: list[str] = []
    notes: list[str] = []  # edition notes, e.g. published without ECMWF 06Z (JSON only, J35)
    verification: VerificationSummary | None = None  # the newest verified date before this one
    mountains: list[MountainReport]

    def mountain(self, key: str) -> MountainReport:
        for m in self.mountains:
            if m.key == key:
                return m
        raise KeyError(key)
