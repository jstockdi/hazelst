# Forecast Method

## Windows (forecast)

The **1d / 3d / 5d** values are **new snow expected to fall**, never past
accumulation. Windows start at **6 AM local on the report day**:

| Window | Span |
|--------|------|
| 1d | next 24 h |
| 3d | next 72 h |
| 5d | next 120 h |

Daily buckets (6 AM → 6 AM) feed the forecast chart.

## Snow amount

- Sum Open-Meteo hourly `snowfall` at **summit elevation**, per model
  (GFS, ECMWF, GEM).
- **Headline value** = **mean** of the three models. The mean is additive, so
  the daily buckets in the chart sum exactly to the window totals in the table
  (a median does not). **Range** = min–max of the per-model window totals.
- **Confidence** from the range width: ≤ 3″ high, 3–6″ medium, > 6″ low.
- **NWS cross-check** (1d and 3d only — NWS snow and ice grids end after ~3
  days): gridpoint values are mm over ISO-8601 intervals (`…/PT6H`); prorate
  each interval into the window and convert to inches. If NWS and the model
  mean differ by more than 50% (and either is ≥ 1″), set `nws_disagrees: true`;
  the narrative must address it.
- **Freezing level** comes from GFS only (ECMWF and GEM do not provide it in
  Open-Meteo).
- Base-elevation snowfall is computed the same way and shown when the rain/snow
  line falls between base and summit.

## Accumulation (observed)

Per mountain: resort-reported new snow for each of the last 7 days, season
total, and base depth. If the resort adapter fails, use NOHRSC 24 h snowfall and
depth and mark `source: "nohrsc"`.

## Pattern tags

Computed by `tags.py` from the numbers, before Claude sees anything. Tags ground
the narrative: Claude may name a pattern only when a tag or an NWS/WPC discussion
supports it.

| Tag | Rule (per mountain, per day) |
|-----|------------------------------|
| `nw_upslope` | 850 hPa wind 270–340°, ≥ 20 kt, RH ≥ 75%, T ≤ -8 °C, for ≥ 6 h |
| `mixing_base` | Precip ≥ 0.01″/h while base-elevation 2 m T > 33 °F |
| `rain_summit` | Precip ≥ 0.01″/h while summit 2 m T > 33 °F |
| `freezing_rain` | 850 hPa T > 32 °F while summit or base 2 m T ≤ 32 °F during precip, or NWS `iceAccumulation` > 0 |
| `wind_hold_watch` / `wind_hold_likely` | Summit gust ≥ `wind_hold_mph.watch` / `.likely` |
| `storm_type` | Track only: `clipper`, `coastal`, `inland_runner`, `none`. From discussion keywords (whitespace-normalized first). It never decides snow vs rain — a "coastal low" on 2026-11-25 brought ice and rain |

Thresholds are starting points; tune them with the verification scores
([agent-runs.md](agent-runs.md): `knowledge-propose` → `config_change`).

## Precip type (per mountain per day, at the summit)

Computed in code and stored in `forecast_day.precip_type`; the page and the
narrative both read it, neither infers it.

| Order | Rule | Type |
|-------|------|------|
| 1 | liquid < 0.05″ and snow < 0.2″ | `dry` |
| 2 | snow ≥ 5 × liquid (half or more fell as ~10:1 snow) | `snow` |
| 3 | `rain_summit` | `rain` |
| 4 | `freezing_rain` | `ice` |
| 5 | otherwise | `mix` |

## Powder score

0–100, for ranking only: 24 h summit snow (50%), summit temp ≤ 20 °F (20%),
gust below the watch threshold (20%), no mixing (10%). Documented in `SKILL.md`
so the ranking and the narrative agree.

## JSON shape (per mountain; stored in `report`, served at `/api/report/<date>.json`)

```json
{
  "key": "killington",
  "forecast": {
    "windows": {
      "1d": { "snow_in": 4, "range": [3, 6], "confidence": "high", "nws_in": 5, "nws_disagrees": false,
              "by_model": { "gfs_seamless": 3, "ecmwf_ifs025": 4, "gem_seamless": 6 } },
      "3d": { "snow_in": 10, "range": [7, 14], "confidence": "medium", "nws_in": 9 },
      "5d": { "snow_in": 13, "range": [8, 19], "confidence": "low", "nws_in": null }
    },
    "days": [
      { "date": "2026-11-26", "snow_in": 4, "hi_f": 26, "lo_f": 14,
        "gust_mph": 32, "freezing_level_ft": 1800, "precip_in": 0.4,
        "precip_type": "snow", "tags": ["nw_upslope"] }
    ],
    "storm_type": "clipper"
  },
  "accumulation": {
    "source": "resort",
    "last7_in": [0, 2, 3, 1, 0, 0, 2],
    "season_in": 25,
    "base_in": 18
  },
  "conditions": { "trails": [22, 155], "lifts": { "open": 6, "hold": 0, "closed": 15 } },
  "alerts": ["Winter Weather Advisory"],
  "powder_score": 71
}
```

Run-level fields: `issued`, `discussions` (`{ "BTV": "...", "ALY": "...",
"GYX": "...", "WPC": "..." }`, Key Messages + Discussion only).
