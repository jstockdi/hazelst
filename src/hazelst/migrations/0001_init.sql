-- 0001_init: every table in docs/design/data-model.md.
-- Applied by hazelst.db.migrate inside one transaction; sets PRAGMA user_version = 1.

-- Reference ---------------------------------------------------------------
CREATE TABLE mountain (
  key TEXT PRIMARY KEY, name TEXT, state TEXT,
  lat REAL, lon REAL, summit_ft INT, base_ft INT,
  nws_office TEXT, nws_grid TEXT,          -- "108,19"
  season_open DATE, season_close DATE,
  config JSON                               -- wind_hold_mph, favored_by, adapter
);

-- Raw ---------------------------------------------------------------------
CREATE TABLE run (
  id INTEGER PRIMARY KEY, kind TEXT,        -- ingest-forecast | ingest-obs | report-build | report-refresh | forecast-verify | skill-learn | knowledge-review | season-backfill
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
  source TEXT,                              -- resort | nohrsc | pns | lsr | model_analysis
  obs_date DATE, reported_at TEXT,
  new_24h_in REAL, new_48h_in REAL, new_7d_in REAL,
  base_in REAL, season_in REAL,
  trails_open INT, trails_total INT, lifts_open INT, lifts_hold INT,
  surface TEXT, raw JSON
);
CREATE TABLE snow_report (                  -- point reports from NWS PNS / LSR
  id TEXT PRIMARY KEY, product_id TEXT, place TEXT,
  lat REAL, lon REAL, elev_ft INT, ending_at TEXT,
  snow_in REAL, hours INT,
  nearest_mountain TEXT, distance_mi REAL
);

-- Derived ------------------------------------------------------------------
CREATE TABLE forecast_window (
  run_id INT, mountain TEXT, window TEXT,   -- 1d | 3d | 5d
  method TEXT,                              -- raw_mean | corrected
  snow_in REAL, lo_in REAL, hi_in REAL, confidence TEXT,
  by_model JSON, base_snow_in REAL, nws_in REAL, nws_ice_in REAL,
  PRIMARY KEY (run_id, mountain, window, method)
);
CREATE TABLE forecast_day (
  run_id INT, mountain TEXT, day DATE,
  snow_in REAL, precip_in REAL, precip_type TEXT,
  hi_f REAL, lo_f REAL, gust_mph REAL, freezing_level_ft REAL,
  tags JSON,                                -- nw_upslope, mixing_base, freezing_rain, ...
  PRIMARY KEY (run_id, mountain, day)
);
CREATE TABLE verification (
  for_date DATE, edition TEXT,              -- 0700 | 0815 | 1440
  mountain TEXT, window TEXT, method TEXT,
  forecast_in REAL, observed_in REAL, observed_source TEXT,
  error_in REAL, ratio REAL, result TEXT,   -- HIT | MISS | NO_OBS
  tags JSON,                                -- tags that fired, copied for slicing
  PRIMARY KEY (for_date, edition, mountain, window, method)
);
CREATE TABLE skill (                        -- rebuilt nightly from verification + forecast_hourly
  mountain TEXT, model TEXT, lead_bucket TEXT, tag TEXT,   -- tag '' = all
  n INT, bias_ratio REAL, mae_in REAL, hit_rate REAL,
  updated_at TEXT,
  PRIMARY KEY (mountain, model, lead_bucket, tag)
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
  status TEXT, created_at TEXT, decided_at TEXT
);

-- Indexes -------------------------------------------------------------------
CREATE INDEX fetch_body_sha256 ON fetch (body_sha256);
CREATE INDEX run_kind_for_date ON run (kind, for_date);
CREATE INDEX agent_call_created_at ON agent_call (created_at);
