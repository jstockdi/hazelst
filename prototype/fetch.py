#!/usr/bin/env python3
"""Prototype fetch for Hazel St Forecast: live sources -> one report JSON.

Stdlib only. Pulls Open-Meteo (three models, summit + base elevation, 7 past
days for modeled accumulation), NWS gridpoints, alerts, and forecast
discussions; computes the 1d/3d/5d forecast windows, daily buckets, and pattern
tags described in docs/design/forecast-method.md.

Usage: python3 prototype/fetch.py [--date YYYY-MM-DD] [--out prototype/out]
"""
import argparse
import datetime as dt
import json
import re
import statistics
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("America/New_York")
UA = "hazelst-forecast-dev"
MODELS = ["gfs_seamless", "ecmwf_ifs025", "gem_seamless"]
OFFICES = ["BTV", "ALY", "GYX"]
FT_PER_M = 3.28084
SUMMIT_VARS = ["snowfall", "precipitation", "temperature_2m", "wind_gusts_10m",
               "freezing_level_height", "snow_depth", "wind_speed_850hPa",
               "wind_direction_850hPa", "temperature_850hPa", "relative_humidity_850hPa"]
BASE_VARS = ["snowfall", "precipitation", "temperature_2m"]


def get_json(url, params=None, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params, safe=",")
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


# --- Open-Meteo -------------------------------------------------------------

def open_meteo(mtns, elev_key, variables):
    params = {
        "latitude": ",".join(str(m["lat"]) for m in mtns),
        "longitude": ",".join(str(m["lon"]) for m in mtns),
        "elevation": ",".join(str(round(m[elev_key] / FT_PER_M)) for m in mtns),
        "hourly": ",".join(variables),
        "models": ",".join(MODELS),
        "timezone": "America/New_York",
        "past_days": 7, "forecast_days": 7,
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
    }
    data = get_json("https://api.open-meteo.com/v1/forecast", params)
    return data if isinstance(data, list) else [data]


def series(loc, var):
    """Per-model hourly lists for one variable; models with no data are dropped."""
    out = {}
    for m in MODELS:
        vals = loc["hourly"].get(f"{var}_{m}")
        if vals and any(v is not None for v in vals):
            out[m] = vals
    return out


def mean_series(per_model):
    """Hourly mean across available models (None where all are missing)."""
    if not per_model:
        return []
    n = len(next(iter(per_model.values())))
    out = []
    for i in range(n):
        vs = [s[i] for s in per_model.values() if s[i] is not None]
        out.append(sum(vs) / len(vs) if vs else None)
    return out


def total(vals, a, b):
    return sum(v for v in vals[a:b] if v is not None)


# --- NWS ----------------------------------------------------------------------

def nws_point(m):
    cache = HERE / "cache" / "points.json"
    cache.parent.mkdir(exist_ok=True)
    known = json.loads(cache.read_text()) if cache.exists() else {}
    if m["key"] not in known:
        p = get_json(f"https://api.weather.gov/points/{m['lat']},{m['lon']}")["properties"]
        known[m["key"]] = {"office": p["gridId"], "x": p["gridX"], "y": p["gridY"]}
        cache.write_text(json.dumps(known, indent=1))
    return known[m["key"]]


def parse_valid(vt):
    """'2026-11-25T12:00:00+00:00/PT6H' -> (start, hours)."""
    start, dur = vt.split("/")
    h = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?", dur)
    hours = int(h.group(1) or 0) * 24 + int(h.group(2) or 0)
    return dt.datetime.fromisoformat(start), max(hours, 1)


def nws_layer_sum(layer, t0, t1, factor):
    """Sum a gridpoint accumulation layer over [t0, t1), prorating partial periods."""
    tot, last_end = 0.0, t0
    for v in layer.get("values", []):
        start, hours = parse_valid(v["validTime"])
        end = start + dt.timedelta(hours=hours)
        last_end = max(last_end, end)
        ov = (min(end, t1) - max(start, t0)).total_seconds() / 3600
        if ov > 0 and v["value"] is not None:
            tot += v["value"] * factor * ov / hours
    return round(tot, 1), last_end >= t1   # (total, grid covers the whole window)


def nws_mountain(m, anchor):
    pt = nws_point(m)
    grid = get_json(f"https://api.weather.gov/gridpoints/{pt['office']}/{pt['x']},{pt['y']}")["properties"]
    mm_to_in = 1 / 25.4
    out = {"office": pt["office"], "grid": f"{pt['x']},{pt['y']}", "snow_in": {}, "ice_in": {}}
    for k, hrs in (("1d", 24), ("3d", 72), ("5d", 120)):
        t1 = anchor + dt.timedelta(hours=hrs)
        snow, full = nws_layer_sum(grid.get("snowfallAmount", {}), anchor, t1, mm_to_in)
        ice, _ = nws_layer_sum(grid.get("iceAccumulation", {}), anchor, t1, mm_to_in)
        out["snow_in"][k] = snow if full else None   # NWS grids stop ~day 3 for snow
        out["ice_in"][k] = ice if full else None
    alerts = get_json("https://api.weather.gov/alerts/active", {"point": f"{m['lat']},{m['lon']}"})
    out["alerts"] = sorted({f["properties"]["event"] for f in alerts.get("features", [])})
    return out


def afd(office):
    graph = get_json(f"https://api.weather.gov/products/types/AFD/locations/{office}")["@graph"]
    prod = get_json(f"https://api.weather.gov/products/{graph[0]['id']}")
    text = prod["productText"]
    # Keep the forecaster's summary sections; drop aviation/marine/hydro tails.
    keep = []
    for sec in re.split(r"\n(?=\.[A-Z][A-Z /]+\.\.\.)", text):
        head = sec.strip().split("...")[0]
        if head in (".WHAT HAS CHANGED", ".KEY MESSAGES", ".DISCUSSION", ".SYNOPSIS",
                    ".NEAR TERM", ".SHORT TERM", ".LONG TERM"):
            keep.append(sec.strip())
    body = "\n\n".join(keep) or text
    return {"office": office, "issued": graph[0]["issuanceTime"], "text": body[:6000]}


STORM_WORDS = [
    ("clipper", r"\bclipper\b"),
    ("coastal", r"nor'?easter|coastal (low|storm)|offshore low|benchmark"),
    ("inland_runner", r"inland runner|lakes cutter|track(s|ing)? (west|through the eastern)|travel northward through the eastern"),
    ("upslope", r"upslope|northwest flow|nw flow|terrain-enhanced"),
    ("freezing_rain", r"freezing rain|\bice accumulation|glaze"),
    ("low_level_jet", r"low[- ]level jet"),
]


def storm_words(texts):
    found = []
    for name, rx in STORM_WORDS:
        offices = [t["office"] for t in texts if re.search(rx, t["text"], re.I)]
        if offices:
            found.append({"tag": name, "offices": offices})
    return found


# --- Assembly -----------------------------------------------------------------

def precip_type(snow_in, precip_in, tags):
    """Summit precip type for one day (docs/design/forecast-method.md, Precip type)."""
    if precip_in < 0.05 and snow_in < 0.2:
        return "dry"
    if snow_in >= 5 * precip_in:
        return "snow"
    if "rain_summit" in tags:
        return "rain"
    if "freezing_rain" in tags:
        return "ice"
    return "mix"


def confidence(lo, hi):
    w = hi - lo
    return "high" if w <= 3 else "medium" if w <= 6 else "low"


def build(date):
    mtns = json.loads((HERE / "mountains.json").read_text())
    anchor_local = dt.datetime.combine(date, dt.time(6, 0), TZ)
    anchor_utc = anchor_local.astimezone(dt.timezone.utc)

    summit = open_meteo(mtns, "summit_ft", SUMMIT_VARS)
    base = open_meteo(mtns, "base_ft", BASE_VARS)
    with ThreadPoolExecutor(8) as ex:
        nws = list(ex.map(lambda m: _safe(nws_mountain, m, anchor_utc), mtns))
        afds = [a for a in ex.map(lambda o: _safe(afd, o), OFFICES) if "error" not in a]

    times = summit[0]["hourly"]["time"]
    i0 = times.index(anchor_local.strftime("%Y-%m-%dT%H:%M"))
    days = [(anchor_local + dt.timedelta(days=d)).strftime("%a %d") for d in range(5)]
    past_days = [(anchor_local - dt.timedelta(days=d)).strftime("%a %d") for d in range(7, 0, -1)]

    out = []
    for m, s, b, nw in zip(mtns, summit, base, nws):
        snow = series(s, "snowfall")
        snow_mean = mean_series(snow)
        precip = mean_series(series(s, "precipitation"))
        t2 = mean_series(series(s, "temperature_2m"))
        gust = mean_series(series(s, "wind_gusts_10m"))
        t850 = mean_series(series(s, "temperature_850hPa"))
        rh850 = mean_series(series(s, "relative_humidity_850hPa"))
        ws850 = mean_series(series(s, "wind_speed_850hPa"))
        wd850 = series(s, "wind_direction_850hPa").get("gfs_seamless", [])
        fz = series(s, "freezing_level_height").get("gfs_seamless", [])
        depth = mean_series(series(s, "snow_depth"))
        base_t2 = mean_series(series(b, "temperature_2m"))
        base_snow = mean_series(series(b, "snowfall"))

        windows = {}
        for k, hrs in (("1d", 24), ("3d", 72), ("5d", 120)):
            per_model = [total(v, i0, i0 + hrs) for v in snow.values()]
            mean = total(snow_mean, i0, i0 + hrs)
            lo, hi = min(per_model), max(per_model)
            windows[k] = {"snow_in": round(mean, 1), "range": [round(lo, 1), round(hi, 1)],
                          "confidence": confidence(lo, hi),
                          "by_model": {mod: round(v, 1) for mod, v in zip(snow, per_model)},
                          "base_snow_in": round(total(base_snow, i0, i0 + hrs), 1),
                          "nws_in": nw.get("snow_in", {}).get(k),
                          "nws_ice_in": nw.get("ice_in", {}).get(k)}
            nws_in = windows[k]["nws_in"]
            windows[k]["nws_disagrees"] = bool(
                nws_in is not None and max(mean, nws_in) >= 1 and
                abs(nws_in - mean) > 0.5 * max(mean, nws_in))

        day_rows = []
        for d in range(5):
            a, z = i0 + 24 * d, i0 + 24 * (d + 1)
            tags = []
            hours = range(a, z)
            wet = [h for h in hours if (precip[h] or 0) >= 0.01]
            upslope = sum(1 for h in hours if wd850 and wd850[h] is not None and 270 <= wd850[h] <= 340
                          and (ws850[h] or 0) >= 23 and (rh850[h] or 0) >= 75 and (t850[h] or 99) <= 17.6)
            if upslope >= 6:
                tags.append("nw_upslope")
            if any((base_t2[h] or 0) > 33 for h in wet):
                tags.append("mixing_base")
            if any((t850[h] or 0) > 32 and min(t2[h] or 99, base_t2[h] or 99) <= 32 for h in wet):
                tags.append("freezing_rain")
            if any((t2[h] or 0) > 33 for h in wet):
                tags.append("rain_summit")
            g = max(v for v in gust[a:z] if v is not None)
            watch, likely = m["wind_hold_mph"]
            if g >= likely:
                tags.append("wind_hold_likely")
            elif g >= watch:
                tags.append("wind_hold_watch")
            temps = [v for v in t2[a:z] if v is not None]
            fzs = [v for v in fz[a:z] if v is not None] if fz else []
            day_rows.append({
                "label": days[d], "snow_in": round(total(snow_mean, a, z), 1),
                "precip_in": round(total(precip, a, z), 2),
                "hi_f": round(max(temps)), "lo_f": round(min(temps)), "gust_mph": round(g),
                "freezing_level_ft_max": round(max(fzs)) if fzs else None, "tags": tags,
                "precip_type": precip_type(total(snow_mean, a, z), total(precip, a, z), tags)})

        past = [round(total(snow_mean, i0 - 24 * (7 - d), i0 - 24 * (6 - d)), 1) for d in range(7)]
        depth_now = depth[i0] if depth and depth[i0] is not None else None
        out.append({
            "key": m["key"], "name": m["name"], "state": m["state"],
            "summit_ft": m["summit_ft"], "base_ft": m["base_ft"],
            "forecast": {"windows": windows, "days": day_rows},
            "accumulation": {"source": "open-meteo-modeled", "last7_in": past,
                             "last7_total_in": round(sum(past), 1),
                             "depth_in": round(depth_now * 39.37, 1) if depth_now is not None else None},
            "alerts": nw.get("alerts", []), "nws_error": nw.get("error"),
        })

    return {
        "date": date.isoformat(),
        "issued": dt.datetime.now(TZ).strftime("%a %d %b %Y · %-I:%M %p"),
        "anchor": anchor_local.isoformat(),
        "days": days, "past_days": past_days, "models": MODELS,
        "discussions": afds, "storm_words": storm_words(afds),
        "mountains": out,
    }


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # a failed source degrades its section, never the run
        return {"error": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.datetime.now(TZ).date().isoformat())
    ap.add_argument("--out", default=str(HERE / "out"))
    a = ap.parse_args()
    report = build(dt.date.fromisoformat(a.date))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{a.date}.json"
    path.write_text(json.dumps(report, indent=1))
    print(path)


if __name__ == "__main__":
    main()
