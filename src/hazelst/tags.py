"""Pattern tags and precip type (forecast-method.md, "Pattern tags" and "Precip type").

Pure functions over one mountain-day of hourly values. Thresholds are
starting points; `config_change` rows tune them later.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from hazelst.models import PrecipType, StormType
from hazelst.sources.discussions import TRACKS

WET_IN_PER_H = 0.01  # an hour is "during precip" at >= 0.01" liquid
WARM_F = 33.0  # 2 m temperature above this while wet -> mixing / rain
FREEZING_F = 32.0
UPSLOPE_DIR = (270.0, 340.0)  # 850 hPa wind direction, degrees
UPSLOPE_MIN_MPH = 23.0  # 20 kt
UPSLOPE_MIN_RH = 75.0
UPSLOPE_MAX_T850_F = 17.6  # -8 C
UPSLOPE_MIN_HOURS = 6
NWS_ICE_MIN_IN = 0.01

TAG_ORDER = (
    "nw_upslope",
    "mixing_base",
    "freezing_rain",
    "rain_summit",
    "wind_hold_likely",
    "wind_hold_watch",
)


@dataclass(frozen=True)
class Hour:
    """Hourly values for one mountain (model mean unless noted)."""

    precip_in: float | None = None
    temp_f: float | None = None  # summit 2 m
    base_temp_f: float | None = None  # base 2 m
    t850_f: float | None = None
    rh850: float | None = None
    ws850_mph: float | None = None
    wd850: float | None = None  # GFS only: directions do not average
    gust_mph: float | None = None


def _or(value: float | None, default: float) -> float:
    return default if value is None else value


def is_upslope(hours: Sequence[Hour]) -> bool:
    """`nw_upslope`: 850 hPa wind 270-340 deg, >= 20 kt, RH >= 75%, T <= -8 C, for >= 6 h."""
    upslope = sum(
        1
        for h in hours
        if h.wd850 is not None
        and UPSLOPE_DIR[0] <= h.wd850 <= UPSLOPE_DIR[1]
        and _or(h.ws850_mph, 0) >= UPSLOPE_MIN_MPH
        and _or(h.rh850, 0) >= UPSLOPE_MIN_RH
        and _or(h.t850_f, 99) <= UPSLOPE_MAX_T850_F
    )
    return upslope >= UPSLOPE_MIN_HOURS


def day_tags(
    hours: Sequence[Hour],
    *,
    wind_watch_mph: float,
    wind_likely_mph: float,
    nws_ice_in: float | None = None,
) -> list[str]:
    """Tags for one 6 AM -> 6 AM day, in `TAG_ORDER`."""
    tags: set[str] = set()
    wet = [h for h in hours if _or(h.precip_in, 0) >= WET_IN_PER_H]
    if is_upslope(hours):
        tags.add("nw_upslope")
    if any(_or(h.base_temp_f, 0) > WARM_F for h in wet):
        tags.add("mixing_base")
    if any(
        _or(h.t850_f, 0) > FREEZING_F
        and min(_or(h.temp_f, 99), _or(h.base_temp_f, 99)) <= FREEZING_F
        for h in wet
    ) or (nws_ice_in is not None and nws_ice_in >= NWS_ICE_MIN_IN):
        tags.add("freezing_rain")
    if any(_or(h.temp_f, 0) > WARM_F for h in wet):
        tags.add("rain_summit")
    gusts = [h.gust_mph for h in hours if h.gust_mph is not None]
    if gusts:
        peak = max(gusts)
        if peak >= wind_likely_mph:
            tags.add("wind_hold_likely")
        elif peak >= wind_watch_mph:
            tags.add("wind_hold_watch")
    return [t for t in TAG_ORDER if t in tags]


def precip_type(snow_in: float, precip_in: float, tags: Iterable[str]) -> PrecipType:
    """Summit precip type for one day; the first rule that matches wins."""
    tag_set = set(tags)
    if precip_in < 0.05 and snow_in < 0.2:
        return "dry"
    if snow_in >= 5 * precip_in:  # half or more of the liquid fell as ~10:1 snow
        return "snow"
    if "rain_summit" in tag_set:
        return "rain"
    if "freezing_rain" in tag_set:
        return "ice"
    return "mix"


def storm_type(words: Iterable[str]) -> StormType:
    """The track named by a discussion's storm words, or `none`.

    Track only: it never decides snow vs rain (forecast-method.md).
    """
    found = set(words)
    for track in TRACKS:
        if track in found:
            return track  # type: ignore[return-value]
    return "none"
