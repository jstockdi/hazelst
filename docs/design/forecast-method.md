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

### Resort ratio (`resort_ratio`, J27)

Open-Meteo's `snowfall` is the model's snow water converted at a fixed 7:1
(0.7 cm per mm). Resort-reported snow runs 9–15 in per inch of forecast snow
water depending on the mountain (decisions row 26), so `resort_ratio`
replaces the fixed 7:1 with a ratio fitted to each resort's own reports
(`ratio.py`):

- **Snow water** per model-hour = `snowfall_in / 7`, which keeps each model's
  rain / snow split (`precip_in` is not used).
- **Cells**: per mountain, per model (and `raw_mean`, the fit of the models'
  mean), per summit 2 m temperature band of the hour (≤ 15 °F, 15–25 °F,
  25–32 °F, > 32 °F; upper bounds inclusive; each model's own temperature),
  and per `nw_upslope` side of the day. Stored in `snow_ratio` with `n`.
- **Fit** (`skill-learn`, nightly, days up to D − 2): ratio of sums over
  lead-day-1 snow days (resort report or the model's forecast ≥ 0.5″). A
  day's resort-reported 6 AM → 6 AM snow is shared among its bands in
  proportion to their forecast snow water; a cell's ratio = its share of
  resort snow ÷ its forecast snow water, clamped to 4–25. A day with no
  forecast snow water is left out; a day counts toward a band's `n` with at
  least 0.01″ of snow water in it.
- **Fallbacks**: the upslope split is used only where the mountain has ≥ 15
  snow days on each side; a split cell with < 15 days uses the mountain's
  own band value; a band with < 15 days uses the all-mountain value for that
  band, then the mountain's all-band value, then the all-mountain all-band
  value. A model with no fit of its own (ECMWF: Previous Runs has no ECMWF
  snowfall) uses the `raw_mean` cells.
- **Forecast**: each model's hourly snow water × its ratio, summed into days
  and windows like the mean: headline = mean of the models in use (stale
  models left out), range = min–max of the per-model `resort_ratio` totals,
  confidence from that range. The days sum to the windows as for the mean.
- **Where it shows**: `report-build` always stores `resort_ratio` windows
  (`forecast_window`) and days (`forecast_day.resort_ratio_in`), and
  `forecast-verify` scores them. At a mountain where `method_gate` chose
  `resort_ratio` ([Method choice](#method-choice-j28)), the headline,
  range, chart, and table use it, labelled "resort ratio", with the mean
  shown beneath; elsewhere the mean stays. `nws_disagrees` compares NWS
  with the value shown.
- **Backfill edition**: a backfilled day's `resort_ratio` is computed with
  ratios fitted on every other season (leave one season out), so a
  backfilled score never reads a ratio fitted on its own day.
- **Backtest**: `hazelst ratio backtest --fit-to D --test-from D [--test-to D]`
  fits on lead-day-1 days through `--fit-to` only and prints, per mountain
  and for all, n, bias ratio, MAE, and hit rate for `raw_mean`, flat 10:1
  (`flat_10`), `resort_ratio` on temperature only (`ratio_temp`), with the
  upslope split (`ratio_upslope`), and `corrected` (skill cells learned
  through `--fit-to`; the raw mean on a day without cells). A test day is a
  mountain-day with a resort report where the report or any method is
  ≥ 0.5″, so every method is scored on the same days. `hazelst ratio show`
  prints the fitted cells and the ratio each one resolves to.

### Method choice (J28)

Each mountain's headline method is chosen on held-out evidence (decisions
rows 28–29; `method.py`), stored in `method_gate`, and read by `report-build`:

- **Candidates**: `raw_mean` and each method `report-build` stores in
  `forecast_window` that the held-out test scores (`resort_ratio` and
  `corrected`), plus the finer-model day-1 candidates (J29, below) whether or
  not a report has stored them yet; a finer candidate with fewer than 15 fit
  days at a mountain is left out there.
- **Held-out season**: seasons run Nov 1 – Apr 30. The test season is the
  newest one that ended before the decision date; the fit uses every day
  through the Apr 30 before it. Scoring is `ratio backtest`'s: the same
  lead-day-1 snow days with a resort report for every method, and the same
  values (`resort_ratio` is the backtest's `ratio_upslope`). On
  2026-11-27 the test season is 2025-26, fitted through 2025-04-30.
- **Live days**: once the current season has ≥ 20 live verified snow days
  at a mountain (1d `verification` rows of editions other than `backfill`;
  a day counts when every candidate scored at the mountain this season has
  a row and the report or a forecast is ≥ 0.5″; a day's error is the mean
  over its editions), that mountain is decided on those days only.
- **Rule**: a headline method is chosen when its 1d MAE is at least 0.05″
  below `raw_mean`'s and lowest among the headline candidates; a tie for
  lowest keeps `raw_mean`. `corrected` never becomes the headline (see
  [Corrected value](#corrected-value-learned)); its row is chosen when its
  MAE is at least 0.05″ below `raw_mean`'s. When the winner is a finer-model
  candidate, it is chosen for day 1 only, and the same rule over the global
  candidates alone picks the method for the windows and days 2–5; both rows
  are chosen (decisions row 30).
- **When**: `hazelst method choose [--date D] [--dry-run]` prints the
  table (column `role`: `headline` for the windows and days, `beneath` for
  `corrected` under the headline, `day1` for a finer-model candidate; blank
  when not chosen) and writes `method_gate`; `skill-learn` re-runs the choice nightly
  after refitting `snow_ratio`. Without a `method_gate` row a mountain
  uses `raw_mean` and shows no corrected value.

### Finer models for day 1 (J29)

The global models (13–25 km) leave `resort_ratio` about 25% low against
resort reports (held-out 2025-26: bias 0.75). Five finer models compete for
day 1 (decisions row 30; data-sources.md, "Finer models (J29)"):
ECMWF IFS HRES 9 km (`ecmwf_ifs`), HRRR CONUS 3 km (`ncep_hrrr_conus`), HRDPS
Continental 2.5 km (`gem_hrdps_continental`), NBM CONUS (`ncep_nbm_conus`,
native snowfall), and AIFS (`ecmwf_aifs025_single`).

- **Collected** every `ingest-forecast` run at summit and base, and
  backfilled from the Historical Forecast API (summit). They never enter the
  model mean, its range, the tags, or `resort_ratio`; those stay on the three
  global models.
- **Ratios**: each finer model gets its own `snow_ratio` cells, fitted as
  `resort_ratio`'s (same bands, upslope split, fallbacks). Snow water is
  `snowfall_in / 7` for every model; for NBM, whose `snowfall` is native
  rather than 7:1, that quantity is a scaled snowfall and its ratio a scale
  factor on it, which works the same way.
- **Day-1 candidates** (`ratio.fine_values`), all lead day 1:
  - `ifs9_ratio`, `hrrr_ratio`, `hrdps_ratio`, `nbm_ratio`, `aifs_ratio`:
    one model's snow water by band × its own resolved ratio;
  - `fine_ratio`: the mean of those over the models with ≥ 15 fit days at the
    mountain (all of them when none has);
  - `nbm_raw`: NBM's native snowfall, as is.
  
  A candidate whose model has fewer than 15 fit days at a mountain (its own
  all-band cell) is reported and left out of `method choose` for that
  mountain (`nbm_raw` with NBM; `fine_ratio` unless at least two finer
  models have enough fit days, since a mean of one model repeats it).
- **Live**: a model counts when its run is at most 18 h old and it covers at
  least 23 hours of day 1. `report-build` stores each candidate as a 1d
  `forecast_window` row (method = the candidate name), and `forecast-verify`
  scores it.
- **Where it shows**: where `method_gate` chose a finer-model candidate for a
  mountain, day 1 and the 1d window take its value, and the 3d and 5d windows
  shift by the same difference, so the chart and the table still agree. Days
  2–5 stay on the mountain's global method (HRRR and HRDPS end 18–48 h after
  their run). The 1d range is the finer models' spread for `fine_ratio`, the
  value itself for a single model. The table names the day-1 method beneath
  the mountain ("day 1: HRRR ratio").
- **Backtest**: `ratio backtest` prints every candidate beside the global
  methods on the same days, the fit days per finer model and mountain, and
  the thin candidates. A test day without a candidate's value scores the raw
  mean for it; the fallback count is printed. The backfilled finer-model
  series is the Historical Forecast's stitched short-lead output, while the
  global models' backfill is Previous Runs `previous_day1` (lead 24–48 h), so
  the backtest favors the finer models by lead time (decisions row 30).

## Corrected value (learned)

Once `skill` has at least 10 verified snow days for a mountain and model at
a lead, `report-build` also stores a `corrected` window (bias-scaled models
blended by 1 / MAE; [data-model.md](data-model.md#how-the-data-compounds),
step 4). The window table shows it as "Corrected N″" under the headline, and
only for a mountain where `method_gate` chose it: the same held-out test
and 0.05″ margin as the headline methods ([Method choice](#method-choice-j28)).
It never becomes the headline, so the chart and the table still agree. `corrected` is computed from the raw models, not from
`resort_ratio`.

## Accumulation (observed)

Per mountain: the new snow the resort reported on each of the 7 days before
the report date, season total, and base depth (`verify.accumulation`), with
the other sources beside each day (decisions row 24).

- Each day (6 AM to 6 AM) takes the resort-reported value the verification
  uses: resort 24 h (non-stale; `resort_llm` only without an adapter value),
  else SnoCountry for the same date, else none (`null`, shown as "no report").
  A day D reads `observation.obs_date` D + 1 (the 24 h ending the morning
  after). `last7_in` holds these values; `days[]` holds each day's value,
  `source`, and `context`: the NOHRSC 24 h value at the pixel and each PNS /
  LSR / GHCN report within 20 mi (`source`, `place`, `snow_in`, `elev_ft`,
  `distance_mi`, `hours`, `ending_at`), the same list `verification.context`
  stores. Context is never summed into the resort value.
- Season total and base depth come from the newest non-stale resort report of
  the last 3 days; without one they are `null` (no NOHRSC fallback).
  `sources` lists every resort-reported source used; the JSON carries them
  and the context sources (the page footer is one line, J35).
- A mountain with neither a resort value nor any context has no accumulation;
  the chart shows only mountains with a resort value, and the section shows
  its empty state when no mountain has 0.5″ or more of resort-reported snow
  in the 7 days.

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

Implementation: `derive.py` (windows, days, NWS proration, stale models),
`tags.py` (tags, precip type, storm type), `ratio.py` (`resort_ratio`), and
`method.py` (method choice).

**Check against the 2026-11-25 sample run** (`tests/unit/test_derive.py`):
rebuilt from the same model runs (GFS 06Z, ECMWF 06Z, GEM 00Z) through the
Single Runs API, derive reproduces the sample's precip type and tags on 39 of
40 mountain-days and the ECMWF window totals exactly. The differences come
from the inputs, not the method: `gfs_seamless` from the Single Runs API
blends the 06Z HRRR rather than the later HRRR the live API served at 10:44
AM, which moves GFS snow totals in the first two days and turns Sugarbush on
Fri 27 from rain to ice; GEM totals match to 0.1″ in 19 of 24 windows. NWS
grids from that morning were not saved, so NWS values are not compared.

## Precip type (per mountain per day, at the summit and at the base)

Computed in code (`tags.day_precip`); the page and the narrative read it,
neither infers it. The same rules run at each elevation with that elevation's
2 m temperature and snow (the liquid and the wet hours are the summit
series'; the two points share a latitude and longitude):

| Order | Rule | Type |
|-------|------|------|
| 1 | liquid < 0.05″ and snow < 0.2″ | `dry` |
| 2 | snow ≥ 5 × liquid (half or more fell as ~10:1 snow) | `snow` |
| 3 | a wet hour above 33 °F at that elevation | `rain` |
| 4 | a wet hour at or below 32 °F there with 850 hPa above 32 °F, or NWS ice ≥ 0.01″ | `ice` |
| 5 | otherwise | `mix` |

- `precip_type` is the summit's (stored in `forecast_day.precip_type`; the
  page's snow grid), `base_precip_type` the base's.
- `freezing_rain_at` lists each elevation that has a freezing-rain hour (rule
  4) on a day that is not `dry` there, so a `rain` day can still carry
  freezing rain at the summit. NWS ice counts at both elevations.
- These three fields are the single source of precip type for a mountain-day
  in the digest (J33, decisions row 33). The tags `mixing_base`,
  `rain_summit`, and `freezing_rain` stay in `forecast_day.tags` and the page
  JSON (skill cells read them) but are not passed to the writer, since a
  mountain-wide `freezing_rain` tag beside a summit `rain` type read as a
  contradiction (2026-11-27, Sugarloaf Monday).
- Before J33 the summit's rule 4 used the colder of the summit and base
  temperatures; re-deriving every production run of 2026-11-26/27 with the
  summit-only rule reproduced every stored `precip_type`.

## Powder score

0–100, for ranking only: 24 h summit snow (50%), summit temp ≤ 20 °F (20%),
gust below the watch threshold (20%), no mixing (10%). Documented in `SKILL.md`
so the ranking and the narrative agree.

## JSON shape (per mountain; stored in `report`, served at `/api/report/<date>.json`)

```json
{
  "key": "killington",
  "forecast": {
    "method": "raw_mean",
    "windows": {
      "1d": { "snow_in": 4, "range": [3, 6], "confidence": "high", "nws_in": 5, "nws_disagrees": false,
              "by_model": { "gfs_seamless": 3, "ecmwf_ifs025": 4, "gem_seamless": 6 } },
      "3d": { "snow_in": 10, "range": [7, 14], "confidence": "medium", "nws_in": 9 },
      "5d": { "snow_in": 13, "range": [8, 19], "confidence": "low", "nws_in": null }
    },
    "days": [
      { "date": "2026-11-26", "snow_in": 4, "hi_f": 26, "lo_f": 14,
        "gust_mph": 32, "freezing_level_ft": 1800, "precip_in": 0.4,
        "precip_type": "snow", "tags": ["nw_upslope"],
        "by_model": { "gfs_seamless": 3.2, "ecmwf_ifs025": 4.1, "gem_seamless": 5.8,
                      "ecmwf_ifs": 4.4, "ncep_hrrr_conus": 3.9 } }
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

`days[].by_model` (J35) is each model's summit snowfall for the day as
Open-Meteo reports it, before any ratio: the global models and the finer
models, each only where its run covers at least 23 hours of the day. It feeds
the page's model drawer and the in-cell model range; the mean, the windows,
and the digest do not read it. Payloads stored before J35 have `{}`.

Run-level fields: `issued`, `discussions` (`{ "BTV": "...", "ALY": "...",
"GYX": "...", "WPC": "..." }`, Key Messages + Discussion only).
