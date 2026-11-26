"""Pydantic models for the forecast numbers (forecast-method.md, "JSON shape")."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

WindowKey = Literal["1d", "3d", "5d"]
Confidence = Literal["high", "medium", "low"]
PrecipType = Literal["snow", "ice", "rain", "mix", "dry"]
StormType = Literal["clipper", "coastal", "inland_runner", "none"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class MountainForecast(_Model):
    key: str
    windows: dict[WindowKey, ForecastWindow]
    days: list[ForecastDay]
    storm_type: StormType


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
