# Data Model (SQLite)

Goal: **keep everything** — every raw response, every model value, every
observation — so the forecast improves as the season goes on. The verification
loop compares what we said with what was reported; the stored history turns
those comparisons into per-mountain, per-model, per-pattern corrections and
local knowledge.

One file: `/data/hazelst.db` on frank (`/srv/hazelst`), WAL mode,
`foreign_keys=ON`. Migrations are numbered, additive SQL files
(`src/hazelst/migrations/NNNN_<name>.sql`), each applied in one transaction at
startup and by `hazelst migrate`; `PRAGMA user_version` holds the last one
applied. A database newer than the code (after a rollback) is used as is.
`0001_init.sql` creates every table below.

**Job lock.** One job at a time: an exclusive `flock` on `<db>.lock` (here
`/data/hazelst.db.lock`), held for the whole job. The kernel releases it when
the process exits, so a crashed job leaves no stale lock. A job that cannot take
the lock writes no `run` row.

## Layers

| Layer | Tables | Written by | Mutable? |
|-------|--------|------------|----------|
| **Raw** | `run`, `fetch` | every job | append-only |
| **Normalized** | `forecast_hourly`, `nws_grid`, `discussion`, `alert`, `observation`, `snow_report` | ingest jobs, from `fetch` | append-only; rebuildable from Raw |
| **Derived** | `forecast_window`, `forecast_day`, `verification`, `skill`, `snow_ratio`, `method_gate` | build / verify / learn jobs | recomputed |
| **Published** | `report`, `agent_call` | build job | append-only (new version per rerun) |
| **Knowledge** | `knowledge`, `config_change` | `knowledge-propose` + owner | status changes only |

Raw is the source of truth: any parser fix can re-derive Normalized and Derived
for the whole season (`hazelst rebuild --since …`).

Job names used in `run.kind` follow [agent-runs.md](agent-runs.md).

## Tables

```sql
-- Reference ---------------------------------------------------------------
CREATE TABLE mountain (
  key TEXT PRIMARY KEY, name TEXT, state TEXT,
  lat REAL, lon REAL, summit_ft INT, base_ft INT,
  nws_office TEXT, nws_grid TEXT,          -- "BTV/108,19": the /points cache
  season_open DATE, season_close DATE,
  config JSON                               -- wind_hold_mph, favored_by, adapter
);

-- Raw ---------------------------------------------------------------------
CREATE TABLE run (
  id INTEGER PRIMARY KEY, kind TEXT,        -- ingest-forecast | ingest-obs | report-build | report-refresh | forecast-verify | skill-learn | knowledge-review | season-backfill | backup
  for_date DATE, started_at TEXT, finished_at TEXT,
  status TEXT, error TEXT,                  -- running | ok | failed
  code_version TEXT                          -- image git SHA
);
CREATE TABLE fetch (
  id INTEGER PRIMARY KEY, run_id INT REFERENCES run,
  source TEXT, url TEXT, params JSON,
  http_status INT, fetched_at TEXT, ms INT,
  body_sha256 TEXT, body BLOB               -- gzip; identical bodies stored once (dedupe on sha)
);

-- Normalized: forecasts ---------------------------------------------------
CREATE TABLE forecast_hourly (
  fetch_id INT REFERENCES fetch, mountain TEXT REFERENCES mountain,
  model TEXT,                               -- gfs_seamless | ecmwf_ifs025 | gem_seamless
  elevation TEXT,                           -- summit | base
  issued_at TEXT,                           -- model run time from Open-Meteo meta.json, not fetch time
  valid_at TEXT, lead_h INT,
  snowfall_in REAL, precip_in REAL, temp_f REAL, gust_mph REAL,
  freezing_level_ft REAL, snow_depth_in REAL,
  t850_f REAL, rh850 REAL, ws850_mph REAL, wd850 REAL,
  PRIMARY KEY (fetch_id, mountain, model, elevation, valid_at)
);
CREATE TABLE nws_grid (
  fetch_id INT, mountain TEXT, layer TEXT,  -- snowfallAmount | iceAccumulation | windGust
  valid_start TEXT, hours INT, value REAL, unit TEXT,
  PRIMARY KEY (fetch_id, mountain, layer, valid_start)
);
CREATE TABLE discussion (
  product_id TEXT PRIMARY KEY, office TEXT, kind TEXT,   -- AFD | PNS | WPC
  issued_at TEXT, text TEXT
);
CREATE TABLE alert (
  id TEXT, mountain TEXT, event TEXT, headline TEXT,
  onset TEXT, expires TEXT, first_seen_run INT,
  PRIMARY KEY (id, mountain)
);

-- Normalized: ground truth ------------------------------------------------
CREATE TABLE observation (                  -- one row per source per mountain per report
  id INTEGER PRIMARY KEY, mountain TEXT, fetch_id INT,
  source TEXT,                              -- resort | resort_llm | snocountry | nohrsc | model_analysis
  obs_date DATE, reported_at TEXT,          -- obs_date D = the 24 h ending the morning of D
  new_24h_in REAL, new_48h_in REAL, new_7d_in REAL,
  base_in REAL, season_in REAL,
  trails_open INT, trails_total INT, lifts_open INT, lifts_hold INT,
  surface TEXT, raw JSON
);
CREATE TABLE snow_report (                  -- point reports: NWS PNS / LSR, GHCN-Daily stations
  id TEXT PRIMARY KEY, product_id TEXT, place TEXT, -- id: "lat,lon,ending_at,snow_in" (lat/lon to 2 dp); GHCN: "ghcn:{station}:{date}", product_id 'GHCN'
  lat REAL, lon REAL, elev_ft INT, ending_at TEXT,  -- elev_ft: station elevation (GHCN), else Open-Meteo elevation API
  snow_in REAL, hours INT,
  nearest_mountain TEXT, distance_mi REAL
);

-- Derived ------------------------------------------------------------------
CREATE TABLE forecast_window (
  run_id INT, mountain TEXT, window TEXT,   -- 1d | 3d | 5d
  method TEXT,                              -- raw_mean | corrected | resort_ratio
  snow_in REAL, lo_in REAL, hi_in REAL, confidence TEXT,
  by_model JSON, base_snow_in REAL, nws_in REAL, nws_ice_in REAL,
  PRIMARY KEY (run_id, mountain, window, method)
);
CREATE TABLE forecast_day (
  run_id INT, mountain TEXT, day DATE,
  snow_in REAL, precip_in REAL, precip_type TEXT,
  hi_f REAL, lo_f REAL, gust_mph REAL, freezing_level_ft REAL,
  tags JSON,                                -- nw_upslope, mixing_base, freezing_rain, ...
  resort_ratio_in REAL,                     -- migration 0006: the day's resort_ratio snow (snow_in is the model mean)
  PRIMARY KEY (run_id, mountain, day)
);
CREATE TABLE verification (
  for_date DATE, edition TEXT,              -- 0700 | 0815 | 1440 | backfill (a day with no report)
  mountain TEXT, window TEXT, method TEXT,  -- window 1d; method raw_mean | corrected | resort_ratio | a model id
  forecast_in REAL, observed_in REAL, observed_source TEXT,  -- resort | resort_llm | snocountry (J26; before: also pns | lsr | ghcn | nohrsc)
  error_in REAL, ratio REAL, result TEXT,   -- HIT | MISS | NO_OBS
  tags JSON,                                -- tags that fired, copied for slicing
  observed_elev_ft INT,                     -- migration 0004: the observation's elevation; NULL since J26 (resort measurement sites are unknown)
  context JSON,                             -- migration 0005: [{source, place, snow_in, elev_ft, distance_mi, hours, ending_at}]: NOHRSC pixel and PNS / LSR / GHCN within 20 mi, never scored
  PRIMARY KEY (for_date, edition, mountain, window, method)
);
CREATE TABLE skill (                        -- rebuilt nightly from verification + forecast_hourly
  mountain TEXT, model TEXT, lead_bucket TEXT, tag TEXT,   -- lead_bucket 1d | 2-3d | 4-5d; tag '' = all
  n INT, bias_ratio REAL, mae_in REAL, hit_rate REAL,
  updated_at TEXT,
  PRIMARY KEY (mountain, model, lead_bucket, tag)
);
CREATE TABLE snow_ratio (                   -- migration 0006; rebuilt nightly by skill-learn (J27)
  mountain TEXT, model TEXT,                -- mountain key or 'all'; model id or 'raw_mean' (the models' mean)
  temp_band TEXT, upslope TEXT,             -- band '<=15' | '15-25' | '25-32' | '>32' (summit 2 m °F), '' = every band; upslope 'yes' | 'no', '' = both
  ratio REAL, n INT,                        -- resort snow per inch of forecast snow water (clamped 4-25); n snow days
  fitted_through DATE,                      -- the last day the fit read (run date D - 2)
  PRIMARY KEY (mountain, model, temp_band, upslope)
);
CREATE TABLE method_gate (                  -- migration 0007; rewritten by `method choose` and nightly by skill-learn (J28)
  mountain TEXT, method TEXT,               -- method raw_mean | a forecast_window method (resort_ratio, corrected)
  test_from DATE, test_to DATE,             -- the days scored: the held-out season, or the current season's live days
  n INT, mae_in REAL, raw_mean_mae_in REAL, -- n snow days; 1d MAE of the method and of raw_mean on the same days
  chosen INT,                               -- 1 = the page's headline method (one per mountain); for corrected, shown beneath the headline
  decided_at TEXT,
  PRIMARY KEY (mountain, method)
);

-- Published ----------------------------------------------------------------
CREATE TABLE report (
  for_date DATE, version INT, run_id INT,
  headline TEXT, best_bet JSON, narrative_md TEXT,
  payload JSON,                             -- the page JSON (forecast-method.md shape)
  status TEXT,                              -- published | numbers_only | failed
  published_at TEXT,
  PRIMARY KEY (for_date, version)
);
CREATE TABLE agent_call (
  id INTEGER PRIMARY KEY, run_id INT, role TEXT,   -- report-write | report-revise | conditions-extract | knowledge-propose
  model TEXT, effort TEXT, sdk_version TEXT,
  prompt_sha TEXT, input_sha TEXT,
  input JSON, output TEXT,
  input_tokens INT, output_tokens INT,
  cache_read_tokens INT, cache_write_tokens INT,
  cost_usd_est REAL,                        -- SDK's client-side list-price estimate; not billed
  ms INT, created_at TEXT,                  -- created_at drives the daily call cap
  status TEXT,                              -- ok | error | invalid
  result_subtype TEXT,                      -- ResultMessage.subtype (success | error_max_turns | ...)
  check_errors JSON
);

-- Knowledge ----------------------------------------------------------------
CREATE TABLE knowledge (
  id INTEGER PRIMARY KEY, mountain TEXT,    -- NULL = regional
  tag TEXT, statement TEXT,                 -- "NW upslope: Sugarbush gets 1.6x model mean (n=9)"
  evidence JSON,                            -- verification keys / run ids
  status TEXT,                              -- proposed | accepted | rejected | retired
  proposed_by TEXT, decided_by TEXT, created_at TEXT, decided_at TEXT
);
CREATE TABLE config_change (                -- threshold / weight changes, same approval flow
  id INTEGER PRIMARY KEY, path TEXT,        -- "mountain.loon.wind_hold_mph"
  old JSON, new JSON, reason TEXT, evidence JSON,
  status TEXT, created_at TEXT, decided_at TEXT,   -- proposed | accepted | rejected
  decided_by TEXT                           -- migration 0002
);

-- Indexes -------------------------------------------------------------------
CREATE INDEX fetch_body_sha256 ON fetch (body_sha256);
CREATE INDEX run_kind_for_date ON run (kind, for_date);
CREATE INDEX agent_call_created_at ON agent_call (created_at);
CREATE UNIQUE INDEX observation_snocountry_day ON observation (mountain, source, obs_date)
  WHERE source = 'snocountry';             -- migration 0003
CREATE INDEX snow_report_ending_at ON snow_report (ending_at);  -- migration 0005
```

## How the data compounds

1. **Every day** adds ~3 forecast issues × 8 mountains × 3 models × 2 elevations ×
   168 h to `forecast_hourly`, plus observations from up to four sources.
2. **forecast-verify** scores each mountain's 1d forecast from each edition
   (7:00, 8:15, 2:40; raw mean and each model)
   against the resort's own reported snow and nothing else (decisions row 24):
   day X (6 AM X to 6 AM X + 1) reads obs_date X + 1, the earliest non-stale
   resort report that day (`resort_llm` only without an adapter value), else
   the `snocountry` row for obs_date X + 1 (the same resort number,
   republished); else `NO_OBS`. PNS, LSR, GHCN, and NOHRSC values for the day
   are recorded beside the score as context (J26), never as the observation:
   `verification.context` lists the NOHRSC 24 h grid ending 12Z on X + 1 at
   the mountain's pixel, and each PNS / LSR / GHCN report within 20 mi of the
   mountain's point (24 h reports ending 4–10 AM on X + 1, GHCN co-op days
   ending 7 AM, and reports of unknown span ending after 6 AM X and by 10 AM
   X + 1), each with place, `snow_in`, `elev_ft`, and `distance_mi`.
   HIT is within 2″ or 30% of the observation.
   `forecast-verify` runs at 9 PM on X + 1 and scores the newest report
   version of each edition, for the mean and each model; rows are replaced
   when a date is re-scored. `hazelst forecast-verify --from D --to D`
   re-scores a range from the stored observations without fetching (after a
   new observation source is backfilled).
3. **skill-learn** (2:00 AM nightly, `learn.py`) rebuilds `skill`: bias ratio
   (sum of forecasts / sum of observations), MAE, and hit rate per mountain ×
   model × lead bucket × pattern tag. A sample is one model's summit snow for
   one 6 AM → 6 AM day from one `forecast_hourly` fetch that covers the whole
   day, at its lead day N = ⌊lead at 6 AM / 24⌋ + 1 (the API's past hours,
   with negative lead, are skipped); fetches of the same lead day are
   averaged. Lead days pool into the window segments `1d` (day 1), `2-3d`, and
   `4-5d`. The same run refits `snow_ratio` (J27, `ratio.py`): resort-reported
   snow per inch of lead-day-1 forecast snow water (`snowfall_in / 7`) per
   mountain × model × summit temperature band × `nw_upslope` side, ratio of
   sums over snow days, clamped to 4–25 (forecast-method.md, "Resort ratio"). The observation and tags are the ones `forecast-verify` scored
   (earliest edition's model-mean row); a day without a `verification` row
   (a backfilled day) takes `verify.observed`. Only snow days count (observed
   or forecast ≥ 0.5″). A run on date D learns from days up to D − 2.
4. **Corrected forecast** (`corrected.py`) — once a cell in `skill` (tag `''`)
   has n ≥ 10 verified snow days, `report-build` adds a `corrected` method:
   in each segment, each model in use is divided by its cell's bias ratio
   (clamped to 0.25–4) and the models with a cell are blended with weights
   ∝ 1 / MAE; the 1d / 3d / 5d windows sum their segments, and a window is
   written only when every segment it spans has a cell. The page shows
   `corrected` (beside the headline, never as it) only for a
   mountain where `method_gate` chose it (step 6). Both stay in the DB
   either way.
5. **Resort ratio** (`ratio.py`) — when `snow_ratio` has cells,
   `report-build` adds a `resort_ratio` method: each model's hourly snow
   water times its resolved ratio, summed into windows and days like the
   mean (mean of the models in use, min–max range). Its days go to
   `forecast_day.resort_ratio_in`. `forecast-verify` scores it beside
   `raw_mean` (live editions from `forecast_window`; the `backfill` edition
   with ratios fitted leaving the day's season out). Where `method_gate`
   chose it (step 6), the page's headline, range, chart, and table use it.
6. **Method choice** (`method.py`, J28) — `hazelst method choose` and the
   nightly `skill-learn` score each candidate method against `raw_mean` on
   the held-out season (fit on earlier seasons, test on the newest complete
   one, the `ratio backtest` days), or on the current season's live
   verified snow days once a mountain has 20, and rewrite `method_gate`. A
   method is chosen when its 1d MAE is at least 0.05″ below `raw_mean`'s
   and lowest (forecast-method.md, "Method choice").
7. **Knowledge** — `knowledge-review` (Monday 3:00 AM) sends `knowledge-propose`
   the `skill` cells with n ≥ 3 (and each cell's values at the previous call,
   read from that call's `agent_call.input`), the week's MISSes with tags and
   discussions, current `knowledge`, and the thresholds. Valid proposals are
   stored with `status = 'proposed'` and `evidence = {keys, agent_call,
   run_id}`; `config_change.old` is the effective value when proposed. The
   owner accepts or rejects them from the CLI; accepted statements go into the
   `report-write` prompt and accepted config changes overlay `mountains.json`.

## Bootstrapping before the first storm

- **Open-Meteo Previous Runs API** gives past forecasts at 1–7 day lead for
  the same coordinates (GFS and GEM snowfall for the 2025–26 season; ECMWF has
  no snowfall at lead).
- **Iowa Environmental Mesonet** archives NWS Local Storm Reports and PNS text
  with snowfall and coordinates — past ground truth for `snow_report`.
- **NOHRSC** 24 h snowfall grids for the same days — context beside each
  score (J26), never the observation.
- **SnoCountry** resort snow history (J23) — the resort-reported values that
  score backfilled days.

All are free and keyless (confirmed 2026-11-26; endpoints in
[data-sources.md](data-sources.md#observation-and-backfill-endpoints)). With
them, `skill` can start with prior seasons instead of zero. `hazelst
season-backfill` loads them into the live tables (details in
[data-sources.md](data-sources.md#season-backfill-j22)); a backfilled day has
no `report` or `verification` rows, and `skill-learn` reads its observation
with `verify.observed`.

## Size

Raw bodies gzip to well under 1 MB per run. ~170 days × ~4 runs ≈ < 1 GB per
season — trivial on frank. No pruning; the history is the point.

## Backup

The DB is the product's memory. The nightly `backup` job copies it with
Python's `sqlite3` online backup API to `/srv/hazelst/backups/hazelst-<date>.db`
and keeps the newest 14 ([hosting-frank.md](../operations/hosting-frank.md#operations)).
