# Sample Run — 2026-11-25

A live end-to-end run of the prototype (`prototype/fetch.py` →
narrative → `prototype/render.py`) against the real sources on
Wed 25 Nov 2026, 10:39 AM ET. Output: `prototype/out/2026-11-25.{json,md,html}`.

## Result

The pipeline produced a correct, useful report for a **no-snow week**: a
coastal low bringing freezing rain Thursday night and rain Friday, matching
all three NWS forecast discussions (BTV, ALY, GYX). Full fetch for 8 mountains
took **~3 seconds**.

The narrative step was written by Claude in the development session from the
JSON and discussions (no API call); production uses `narrative.py`.

## Findings → spec changes

| # | Finding | Evidence | Spec change |
|---|---------|----------|-------------|
| 1 | Open-Meteo returns all 8 mountains × 3 models in one call (a JSON list); `elevation` is meters; `precipitation_unit=inch` makes `snowfall` inches; `past_days=7` works | `om.json` probe; fetch.py | data-sources.md: one call per elevation |
| 2 | `freezing_level_height` exists for GFS only (ECMWF, GEM return `undefined`) | `hourly_units` | forecast-method.md: freezing level from GFS; mixing from base temperature |
| 3 | Median of window totals does not equal the sum of daily medians, so the chart and the table disagree | design check | forecast-method.md: use the **model mean** (additive); range = min–max of per-model totals |
| 4 | NWS gridpoint `snowfallAmount` / `iceAccumulation` cover ~3 days, so the 5-day NWS value is always empty | `nws_in` 5d = null | forecast-method.md: NWS cross-check on 1d and 3d only |
| 5 | NWS values are mm over ISO-8601 intervals (`…/PT6H`) and must be prorated into the 6 AM windows | fetch.py `nws_layer_sum` | forecast-method.md |
| 6 | Discussion text wraps lines mid-phrase; a keyword match failed on "travel northward through the \neastern" | storm_words output | narrative.md: normalize whitespace before matching |
| 7 | GYX called it a "coastal low" — but a warm one. Track words do not say whether it snows | GYX AFD vs model temps | forecast-method.md: `storm_type` describes the track only; snow vs ice vs rain comes from `precip_type` |
| 8 | The first narrative draft got the precip type wrong at 3 mountains (Cranmore, Killington, Sunday River said rain/mix; the data said ice) | render vs md | forecast-method.md: compute `precip_type` per day in the JSON; narrative.md: deterministic claim check before publish |
| 9 | With no snow forecast, the snow chart is nearly empty and tells nothing | render | report-page.md: add a "What falls" precip-type grid (snow / ice / rain / mix / dry + liquid inches) |
| 10 | Liftie returns **403 (Cloudflare bot challenge)** to scripts | `curl` 403, `cf-mitigated: challenge` | data-sources.md: Liftie dropped; self-host it on frank or parse lift status in resort adapters |
| 11 | Resort sites: 5 of 8 return 200, 3 redirect; no shared format | `curl` probe | data-sources.md: adapters are the main build risk |
| 12 | Modeled accumulation shows natural snow only (0″ this week) and no base depth — snowmaking bases are invisible to models | `past7` = 0, `snow_depth` = 0 | report-page.md: accumulation empty state; resort reports required for base |
| 13 | NWS Albany has a wind-gust Key Message (> 40 mph Thu) matching the model gusts (44 mph at Stratton) | ALY AFD | Confirms the gust thresholds; no change |
| 14 | Pico's season status had to come from outside the data | narrative | mountains.md: add `season_open` |
