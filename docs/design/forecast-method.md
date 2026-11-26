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
  Open-Meteo); the day value is the GFS maximum. The 850 hPa wind direction
  used by `nw_upslope` is GFS too (directions do not average); every other
  hourly input is the mean of the models in use.
- **Stale models**: a model whose run (`forecast_hourly.issued_at`) is more
  than 18 h old when the report is built is listed in `by_model` and flagged,
  but left out of the mean, the range, and the hourly means. If every model is
  stale, all are used and all are flagged.
- **Rounding**: window and daily snow to 0.1″, liquid to 0.01″, temperatures
  and gusts to whole numbers. The 1d window equals day 1 exactly; the 3d and
  5d windows equal the sum of the rounded days to within rounding
  (≤ 0.05″ per day).
- Base-elevation snowfall is computed the same way and shown when the rain/snow
  line falls between base and summit.

## Corrected value (learned)

Once `skill` has at least 10 verified snow days for a mountain and model at
a lead, `report-build` also stores a `corrected` window (bias-scaled models
blended by 1 / MAE; [data-model.md](data-model.md#how-the-data-compounds),
step 4). The window table shows it as "Corrected N″" under the mean, and only
for a mountain where `corrected` has beaten the mean on its last 20 verified
days. The mean stays the headline, so the chart and the table still agree.

## Accumulation (observed)

Per mountain: new snow observed on each of the 7 days before the report date,
season total, and base depth (`verify.accumulation`).

- Each day (6 AM to 6 AM) takes the best observation by the verification
  priority: resort 24 h (non-stale; `resort_llm` only without an adapter
  value) > PNS / LSR within 5 mi > NOHRSC 24 h > none (`null`). A day D reads
  `observation.obs_date` D + 1 (the 24 h ending the morning after).
- Season total and base depth come from the newest non-stale resort report of
  the last 3 days; without one, the NOHRSC season-to-date grid (no base depth),
  and `source` is `"nohrsc"`. `sources` lists every source used; the footer
  names them.
- A mountain with no observation at all has no accumulation; the section shows
  its empty state when no mountain has 0.5″ or more in the 7 days.

## Pattern tags

Computed by `tags.py` from the numbers, before Claude sees anything. Tags ground
the narrative: Claude may name a pattern only when a tag or an NWS/WPC discussion
supports it.

| Tag | Rule (per mountain, per day) |
|-----|------------------------------|
| `nw_upslope` | 850 hPa wind 270–340°, ≥ 20 kt, RH ≥ 75%, T ≤ -8 °C, for ≥ 6 h |
| `mixing_base` | Precip ≥ 0.01″/h while base-elevation 2 m T > 33 °F |
| `rain_summit` | Precip ≥ 0.01″/h while summit 2 m T > 33 °F |
| `freezing_rain` | 850 hPa T > 32 °F while summit or base 2 m T ≤ 32 °F during precip, or NWS `iceAccumulation` prorated into the day ≥ 0.01″ |
| `wind_hold_watch` / `wind_hold_likely` | Summit gust ≥ `wind_hold_mph.watch` / `.likely` |
| `storm_type` | Track only: `clipper`, `coastal`, `inland_runner`, `none`. From the keywords of the mountain's NWS office AFD (whitespace-normalized first); the first of `clipper`, `coastal`, `inland_runner` found wins. It never decides snow vs rain — a "coastal low" on 2026-11-25 brought ice and rain |

Thresholds are starting points; tune them with the verification scores
([agent-runs.md](agent-runs.md): `knowledge-propose` → `config_change`).

Implementation: `derive.py` (windows, days, NWS proration, stale models) and
`tags.py` (tags, precip type, storm type).

**Check against the 2026-11-25 sample run** (`tests/unit/test_derive.py`):
rebuilt from the same model runs (GFS 06Z, ECMWF 06Z, GEM 00Z) through the
Single Runs API, derive reproduces the sample's precip type and tags on 39 of
40 mountain-days and the ECMWF window totals exactly. The differences come
from the inputs, not the method: `gfs_seamless` from the Single Runs API
blends the 06Z HRRR rather than the later HRRR the live API served at 10:44
AM, which moves GFS snow totals in the first two days and turns Sugarbush on
Fri 27 from rain to ice; GEM totals match to 0.1″ in 19 of 24 windows. NWS
grids from that morning were not saved, so NWS values are not compared.

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
