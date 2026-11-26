# Data Sources

Rule: **every source is fetched and used as data.** No link-only sources — if
the pipeline cannot read it into the JSON, it is not in the report.

All sources below are free and keyless (NWS requires a `User-Agent` header).

## Forecast

| Source | Endpoint | Fields used | Feeds |
|--------|----------|-------------|-------|
| **Open-Meteo Forecast** | `api.open-meteo.com/v1/forecast` — one call per elevation for all mountains (comma-separated `latitude`/`longitude`/`elevation` in meters; response is a list); `precipitation_unit=inch`, `past_days=7` | Hourly `snowfall`, `precipitation`, `temperature_2m`, `wind_speed_10m`, `wind_gusts_10m`, `freezing_level_height`; pressure-level `wind_direction_850hPa`, `wind_speed_850hPa`, `temperature_850hPa`, `relative_humidity_850hPa`. `elevation=` set to summit, then base. Models: GFS/HRRR, ECMWF IFS, GEM. | 1d/3d/5d snow, ranges, temps, gusts, mixing, upslope tag |
| **NWS gridpoint** | `api.weather.gov/gridpoints/{office}/{x},{y}` (grid from `/points/{lat},{lon}`, cached) | `snowfallAmount`, `iceAccumulation` (mm, ISO-8601 intervals, ~3 days), `windGust` | Cross-check of 1d/3d totals; ice |
| **NWS alerts** | `api.weather.gov/alerts/active?point={lat},{lon}` | Event, headline, onset/expires | Cautions section |
| **NWS Area Forecast Discussion** | `api.weather.gov/products/types/AFD/locations/{BTV,ALY,GYX}` | Key Messages + Discussion text | Narrative: storm type, timing, mixing, confidence |
| **WPC discussions** | `api.weather.gov/products/types/{PMD,QPF}/...` (confirm product codes) | Short-range and winter-weather discussion text | Narrative: national storm track |

### Open-Meteo request details (verified 2026-11-26)

- Request: `timezone=GMT` (stored times are UTC; a local-time response uses one
  offset for the whole series, which is wrong across a DST change),
  `models=gfs_seamless,ecmwf_ifs025,gem_seamless`, `past_days=7`,
  `forecast_days=7`, `temperature_unit=fahrenheit`, `wind_speed_unit=mph`,
  `precipitation_unit=inch`. The same hourly variables are requested at summit
  and base.
- Units follow `hourly_units`, which normalize reads per variable: with these
  options `snowfall` and `precipitation` are inches, but `snow_depth` and
  `freezing_level_height` are **feet** (stored as inches and feet).
  `freezing_level_height` is `undefined` for ECMWF and GEM.
- Model run times: `api.open-meteo.com/data/{id}/static/meta.json`, fetched in
  every ingest and stored as `fetch` rows (`source = open_meteo_meta`).
  `last_run_initialisation_time` becomes `forecast_hourly.issued_at`.

  | Model | meta.json id |
  |-------|--------------|
  | `gfs_seamless` | `ncep_gfs013` (GFS run; HRRR blends into the first hours) |
  | — | `ncep_hrrr_conus` (recorded, not used for `issued_at`) |
  | `ecmwf_ifs025` | `ecmwf_ifs025` |
  | `gem_seamless` | `cmc_gem_gdps` |

- `cmc_gem_gdps/static/meta.json` stopped updating on 2026-05-26 (its
  `data_end_time` is 2026-06-05) while GEM data in the forecast response stays
  current. When a meta.json's `data_end_time` is before the fetch, its run time
  cannot describe the data served, so the run is **estimated** from the
  model's own cycle: the latest initialisation on `update_interval_seconds`
  that is available by the fetch time given the file's last observed delay
  (`last_run_availability_time − last_run_initialisation_time`). The ingest
  summary marks such run times `estimated: true`.
- Raw `fetch` rows: `params` is `{"query": {…}, "meta": {…}}`; for forecasts
  `meta` holds the elevation and the mountain keys in request order (the
  response list follows that order).
- Fixtures: `hazelst record open-meteo` writes each raw response to
  `tests/fixtures/open_meteo/<date>/<label>.json.gz`.

## Accumulation and conditions

| Source | Endpoint | Fields used | Feeds |
|--------|----------|-------------|-------|
| **Resort mountain reports** | Four shared presets, one config entry per resort (see [Resort adapters](#resort-adapters)) | New 24 h / 48 h / 7-day snow, base depth, season total, trails and lifts open, lifts on hold | Accumulation chart, per-mountain notes |
| **NWS snowfall reports (PNS)** | Live: `api.weather.gov/products/types/PNS/locations/{BTV,ALY,GYX}`; archive: IEM AFOS (see [Observation endpoints](#observation-and-backfill-endpoints)) | `**METADATA**` lines: place, lat/lon, `SNOW_24` or `SNOW` (storm total), inches, provider | Accumulation cross-check; verification; season backfill |
| **NOAA NOHRSC** | National Snowfall Analysis v2 GeoTIFF, `www.nohrsc.noaa.gov/snowfall_v2/data/{YYYYMM}/sfav2_CONUS_24h_{YYYYMMDD}12.tif` | 24 h, 48 h, 72 h, and season-to-date snowfall at each mountain's pixel | Accumulation fallback; verification |
| **Iowa Environmental Mesonet** | LSR GeoJSON `mesonet.agron.iastate.edu/geojson/lsr.php`; PNS text archive `api/1/nws/afos/list.json` + `api/1/nwstext/{product_id}` | Point snowfall reports with coordinates, current and historical | Ground truth; season backfill |
| **Open-Meteo Previous Runs** | `previous-runs-api.open-meteo.com/v1/forecast`, `{var}_previous_day{N}` | Past forecasts at 1–7 day lead for GFS and GEM; ECMWF precipitation and temperature only | Backfill `skill` before the first storm |
| **Open-Meteo Historical Forecast** | `historical-forecast-api.open-meteo.com/v1/forecast` | Short-lead model output stitched from successive runs, 2025–26 season | Not used for `skill` (wrong lead time); available for `model_analysis` observations |
| **NWS station observations** | `api.weather.gov/stations/{id}/observations` | Base-area temp, wind, precip type (e.g. KRUT, KMPV, KBML) | "Right now" context |
| **Mount Washington Observatory** | Higher Summits Forecast (text) | Summit wind and temperature, Presidentials | NH/ME wind context |

Lift status comes from the resort adapters (Liftie is blocked; see Excluded).

Resort report adapters are the fragile part: no resort publishes an official
API. Models see only natural snow, so base depth and open terrain exist only in
resort reports. Each adapter is isolated, has a fixture-based test, and on
failure the report falls back to NOHRSC for accumulation and says so in the
footer. Robots and content signals are in the [resort map](#resort-report-map)
(checked 2026-11-26). Loon, Sunday River, and Sugarloaf (Boyne) publish
`Content-Signal: search=yes,ai-train=no,use=reference` with no `ai-input`
signal — their adapters are code-only and their content is never sent to
Claude. All adapters fetch at most 3 times a day with an identifying
`User-Agent`.

## Resort adapters

Mapped 2026-11-26 (job J00). Fixtures and per-resort notes:
`tests/fixtures/resorts/<key>/`. Every resort is covered by one of four
presets; no resort needs its own code.

### Resort report map

| Key | Report page → final URL | robots.txt / content signal | Form | Data URL | Preset |
|-----|------------------------|-----------------------------|------|----------|--------|
| `killington` | `www.killington.com/the-mountain/conditions-weather/current-conditions-weather` → `killington.com/mountain-report` (200) | `/mountain-report` allowed; no signal | `html` | `https://killington.com/mountain-report` | `dor_html` |
| `pico` | `www.picomountain.com/the-mountain/conditions-weather/current-conditions-weather` → `picomountain.com/mountain-report` (200) | allowed (same file as Killington); no signal | `html` | `https://picomountain.com/mountain-report` | `dor_html` |
| `sugarbush` | `www.sugarbush.com/mountain/conditions` (200, no redirect) | allowed; no signal. Feed hosts `v4.mtnfeed.com`, `mtnpowder.com` allow `/resorts/` and `/feed/v3.json`; no signal | `json` | `https://mtnpowder.com/feed/v3.json?bearer_token={token}&resortId[]=70` | `mtnpowder` |
| `stratton` | `www.stratton.com/the-mountain/mountain-report` (200, but the body is an Imperva bot challenge) | unknown: `robots.txt` also returns the challenge. The adapter never fetches `stratton.com`; feed hosts as Sugarbush | `json` | `https://mtnpowder.com/feed/v3.json?bearer_token={token}&resortId[]=1` | `mtnpowder` |
| `loon` | `www.loonmtn.com/mountain-report` (200, no redirect) | `Allow: /`; `Content-Signal: search=yes,ai-train=no,use=reference` | `json` | `https://www.loonmtn.com/api/reportpal?resortName=lm&useReportPal=true` | `boyne_reportpal` |
| `cranmore` | `cranmore.com/snow-report` (`www.` redirects to the bare host; 200) | only `/cpresources/` disallowed; no signal | `html` | `https://cranmore.com/snow-report` | `cranmore_html` |
| `sunday_river` | `www.sundayriver.com/mountain-report` (200, no redirect) | as Loon | `json` | `https://www.sundayriver.com/api/reportpal?resortName=sr&useReportPal=true` | `boyne_reportpal` |
| `sugarloaf` | `www.sugarloaf.com/mountain-report` (200, no redirect) | as Loon | `json` | `https://www.sugarloaf.com/api/reportpal?resortName=sl&useReportPal=true` | `boyne_reportpal` |

How each data URL was found:

- **Boyne** (`boyne_reportpal`): the site JS calls `GET /api/reportpal?resortName=<code>&useReportPal=true`
  on the resort's own host; a plain GET with no extra headers returns JSON.
- **Alterra** (`mtnpowder`): the report page sets
  `liftsAndTrailsConfig.resortPath` and loads the `v4.mtnfeed.com` widget,
  which reads `https://v4.mtnfeed.com/resorts/<resortPath>.json`
  (`bearerToken`, `resortIds`) and then calls the `mtnpowder.com` feed. The
  token is a public client token served to every visitor; the adapter reads it
  from the widget config at run time instead of storing it, so a rotation needs
  no change.
- **Killington and Pico** (`dor_html`): one server-rendered CMS; no JSON
  endpoint (the only script bundle loads autocomplete and event data) and the
  page's `ld+json` is static resort information.
- **Cranmore** (`cranmore_html`): server-rendered; the JS bundle fetches
  nothing and no third-party widget carries the report.

### Field coverage

`✓` = present in the source; `—` = not published.

| Field | `boyne_reportpal` | `mtnpowder` | `dor_html` | `cranmore_html` |
|-------|-------------------|-------------|------------|-----------------|
| `new_24h_in` | ✓ | ✓ | ✓ | ✓ (labelled "Overnight") |
| `new_48h_in` | ✓ (Loon's was `null`) | ✓ | ✓ | — |
| `new_7d_in` | ✓ | ✓ | ✓ | ✓ |
| `base_in` | ✓ | ✓ | — | ✓ (range, e.g. `26-40"`) |
| `season_in` | ✓ | ✓ | ✓ | ✓ |
| `trails_open` / `trails_total` | ✓ | ✓ | ✓ | ✓ |
| `lifts_open` / `lifts_total` | ✓ | ✓ | ✓ | ✓ |
| `lifts_hold` | ✓ (`numLiftsOnHold`) | — | — | — |
| `reported_at` | ✓ UTC | ✓ with offset | ✓ local time | date only |

### Adapter spec

Each mountain's `report_adapter` entry in `mountains.json` names a preset and
its parameters ([mountains.md](../product/mountains.md#report-adapters)):

```json
"report_adapter": { "preset": "boyne_reportpal", "params": { "host": "www.loonmtn.com", "code": "lm" } }
```

A preset is a declarative record with a `kind`, a URL template, and a field
map. Three small shared parsers serve every preset, one per kind:

| Kind | Parser input | Field map entry |
|------|-------------|-----------------|
| `json` | Response JSON | A dotted path with list indices (`a.b[0].c`), or a list of paths where the first non-null wins |
| `embedded_json` | A `<script>` selected by CSS (`script#__NEXT_DATA__`, `script[type="application/ld+json"]`), parsed as JSON, then as `json` | As `json`. No resort uses it today; it stays for a site that moves its data inline |
| `html` | `selectolax` tree, optionally narrowed by `scope` | `{"select": css}` for one element's text, or `{"label": text}` matched against a `labels` block: each element matching `labels.label` whose text equals `text` pairs with its next sibling matching `labels.value` |

A field map entry may add `"parse"` with one shared value parser:

| Parser | Input → output |
|--------|----------------|
| `inches` (default for snow and base fields) | `"4"`, `"4\""`, `"1.5"` → float; `"--"`, `""`, `null` → `null` |
| `range_max` | `"26-40\""` → 40.0 (upper bound; the raw string stays in `observation.raw`); a single value parses as `inches` |
| `ratio` | `"55/62"` → `(55, 62)`, filling the `_open` and `_total` pair |
| `int` | integer or numeric string |
| `datetime` | ISO-8601 (`Z`, `-05:00`, or `-0500`), or `"format"` (strptime) plus `"tz"` for local times; `"regex"` with one group extracts it from surrounding text |

The four presets:

```json
{
  "boyne_reportpal": {
    "kind": "json",
    "url": "https://{host}/api/reportpal?resortName={code}&useReportPal=true",
    "fields": {
      "new_24h_in":   "currentConditions.resortLocations.location[0].snow24Hours.inches",
      "new_48h_in":   "currentConditions.resortLocations.location[0].snow48Hours.inches",
      "new_7d_in":    "currentConditions.resortLocations.location[0].snow7Days.inches",
      "base_in":      "currentConditions.resortLocations.location[0].base.inches",
      "season_in":    "currentConditions.resortLocations.location[0].snowSeasonTotal.inches",
      "trails_open":  "currentConditions.resortwide.numTrailsOpen",
      "trails_total": "currentConditions.resortwide.numTrailsTotal",
      "lifts_open":   "currentConditions.resortwide.numLiftsOpen",
      "lifts_total":  "currentConditions.resortwide.numLiftsTotal",
      "lifts_hold":   "currentConditions.resortwide.numLiftsOnHold",
      "reported_at":  { "path": "updated", "parse": "datetime" }
    }
  },
  "mtnpowder": {
    "kind": "json",
    "prefetch": { "url": "https://v4.mtnfeed.com/resorts/{feed_key}.json", "vars": { "token": "bearerToken" }, "cache_hours": 24 },
    "url": "https://mtnpowder.com/feed/v3.json?bearer_token={token}&resortId[]={resort_id}",
    "fields": {
      "new_24h_in":   "Resorts[0].SnowReport.AllMountain.Last24HoursIn",
      "new_48h_in":   "Resorts[0].SnowReport.AllMountain.Last48HoursIn",
      "new_7d_in":    "Resorts[0].SnowReport.AllMountain.Last7DaysIn",
      "base_in":      { "path": ["Resorts[0].SnowReport.SnowBaseRangeIn", "Resorts[0].SnowReport.BaseArea.BaseIn"], "parse": "range_max" },
      "season_in":    "Resorts[0].SnowReport.SeasonTotalIn",
      "trails_open":  "Resorts[0].SnowReport.TotalOpenTrails",
      "trails_total": "Resorts[0].SnowReport.TotalTrails",
      "lifts_open":   "Resorts[0].SnowReport.TotalOpenLifts",
      "lifts_total":  "Resorts[0].SnowReport.TotalLifts",
      "reported_at":  { "path": "Resorts[0].SnowReport.LastUpdate", "parse": "datetime" }
    }
  },
  "dor_html": {
    "kind": "html",
    "url": "https://{host}/mountain-report",
    "scope": "main#page",
    "labels": { "label": ".dor-snow-label", "value": ".dor-snow-val" },
    "fields": {
      "new_24h_in":  { "label": "24 Hour" },
      "new_48h_in":  { "label": "48 Hour" },
      "new_7d_in":   { "label": "7 Day" },
      "season_in":   { "label": "Season" },
      "trails":      { "select": "a.dor-bar-row[aria-label='Open Trails'] .dor-bar-count", "parse": "ratio" },
      "lifts":       { "select": "a.dor-bar-row[aria-label='Open Lifts'] .dor-bar-count", "parse": "ratio" },
      "reported_at": { "select": ".dor-snow-report > .condition-report-date", "parse": "datetime",
                       "regex": "Updated (.+)", "format": "%B %d, %Y %I:%M %p", "tz": "America/New_York" }
    }
  },
  "cranmore_html": {
    "kind": "html",
    "url": "https://cranmore.com/snow-report",
    "scope": "section.margin",
    "labels": { "label": ".datapoint h3", "value": "span.value" },
    "fields": {
      "new_24h_in":  { "label": "Overnight" },
      "new_7d_in":   { "label": "7 Day Total" },
      "base_in":     { "label": "Base Depth", "parse": "range_max" },
      "season_in":   { "label": "Season Total" },
      "trails":      { "label": "Open Trails", "parse": "ratio" },
      "lifts":       { "label": "Open Lifts", "parse": "ratio" },
      "reported_at": { "select": "section.margin", "parse": "datetime",
                       "regex": "Last Updated:\\s*(\\w{3} \\d{1,2}, \\d{4})", "format": "%b %d, %Y", "tz": "America/New_York" }
    }
  }
}
```

Rules shared by every preset:

- A label that is absent means "not reported" (`null`), never zero. Cranmore
  hides its snow datapoints before opening day.
- `scope` matters on Killington and Pico: the header menu repeats the snow grid
  and status bars. Status bars are picked by `aria-label`, not position, because
  their order differs between the two sites.
- `dor_html` has two dates: `.condition-report-date` inside `.dor-snow-report`
  is the snow-report time; the one inside `section.condition-report` is the
  narrative report and is not used.
- `mtnpowder` top-level `LastUpdate` and `Resorts[0].LastUpdate` are feed refresh
  times; only `SnowReport.LastUpdate` is the report time. Snow values are
  strings and `"--"` means no value.
- `boyne_reportpal` trail counts come from `resortwide`, which counts alpine
  trails only (Loon's per-trail list includes open Nordic trails). Location
  `[0]` is the alpine area on all three sites. The adapter reads numbers only;
  the `comments` text is never stored in a field passed to Claude.
- Staleness: a report whose `reported_at` is more than 36 hours old is stored
  but flagged `stale` in `observation.raw`, and `forecast-verify` ignores its
  new-snow fields. On 2026-11-26 Killington and Pico showed the same snow grid
  stamped 2026-11-15, so the two sites may share one snow record.
- Block detection: a 200 response that does not parse (for example Stratton's
  Imperva challenge page, kept as `stratton/report_blocked.html.gz`) counts as
  an adapter failure, not as zeros.

Fetch budget per run: one data URL per resort; `mtnpowder` adds one widget
config fetch per resort per day. No adapter fetches the Boyne or Alterra report
pages.

### Gaps and fallbacks

| Gap | Resorts | Handling |
|-----|---------|----------|
| No base depth | Killington, Pico | `base_in` is `null`; the page shows "—" for base |
| No 48 h snow | Cranmore | `new_48h_in` is `null`; 48 h accumulation uses NOHRSC |
| Report date without a time | Cranmore | `reported_at` is midnight local; staleness uses the date |
| "Overnight" instead of 24 h | Cranmore | Stored as `new_24h_in` |
| Report site blocked | Stratton (`stratton.com`) | Not needed: the `mtnpowder` feed carries the data. If the feed fails, the Fallbacks rule applies (adapter disabled; PNS / LSR / NOHRSC) |
| Off-season values | All | Fixtures are early season: every snow value was `0` or `--`; decimal and range formats beyond Cranmore's archived copy are unconfirmed |

## Observation and backfill endpoints

Confirmed 2026-11-26 with one live request each. Fixtures:
`tests/fixtures/{iem,nws_pns,nohrsc,open_meteo_historical}/`.

### IEM Local Storm Reports

`GET https://mesonet.agron.iastate.edu/geojson/lsr.php?wfos=BTV,ALY,GYX&sts=YYYYMMDDHHMM&ets=YYYYMMDDHHMM`
(UTC). Returns a GeoJSON `FeatureCollection`. Snow reports have
`properties.type == "S"` (`typetext` `SNOW`); fields used: `magf` (float
inches), `valid` (UTC ISO-8601), `lat`, `lon`, `city`, `county`, `st`,
`source`, `remark`, `product_id`. Hours are not given (`snow_report.hours` is
`null`). 2026-02-01 to 2026-02-08: 216 features, 212 of them snow. The window
2026-11-19 to 2026-11-26 returned an empty collection (no reports). Works for
the 2025–26 season, so it serves J22.

### NWS PNS snowfall reports

- Live: `GET https://api.weather.gov/products/types/PNS/locations/{BTV|ALY|GYX}`,
  then `GET https://api.weather.gov/products/{id}` → `productText`. Retention
  is short: on 2026-11-26 BTV and GYX listed nothing and ALY listed one
  non-snow PNS.
- Archive: `GET https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json?pil=PNS{BTV|ALY|GYX}&date=YYYY-MM-DD`
  → `data[].product_id`, then `GET https://mesonet.agron.iastate.edu/api/1/nwstext/{product_id}`
  → the same text. Used by J22 and as the live fallback.
- Parse only the `**METADATA**` block; skip a PNS without one. Each line is
  comma-separated: `:M/DD/YYYY,HHMM AM, ST, County, Place, , , lat, lon, TYPE, value, Inch, Provider, description,`.
  `TYPE` is `SNOW_24` (24 h, `hours` = 24) or `SNOW` (storm total, `hours`
  `null`). Times are local to the office (all three use America/New_York).
  Clean `null` tokens in place and provider (GYX wrote ` null Portland Jetport`).

### Choosing reports near each mountain

For each LSR or PNS point, compute the great-circle distance to every mountain
in `mountains.json`; store `nearest_mountain` and `distance_mi` in
`snow_report`. `forecast-verify` uses reports within 5 mi of the mountain
(data-model.md, observation priority). Reports are mostly valley spotters and
read lower than summits; elevation is not given (`elev_ft` is `null`). The same
CoCoRaHS observation can appear in both an LSR and a PNS: deduplicate on
`(lat, lon)` rounded to 2 decimals, `ending_at`, and `snow_in`, keeping the PNS
row. LSR coordinates carry only two decimals (PNS carries four), so a
3-decimal key matched 1 of the 38 BTV reports on 2026-02-07 that also appear
in an LSR; 2 decimals matches all 38. The key is the `snow_report.id`
(`44.30,-73.98,2026-02-07T12:00:00+00:00,3`): a PNS row replaces a row with the
same id, and an LSR row never replaces one.

### NOHRSC National Snowfall Analysis

`GET https://www.nohrsc.noaa.gov/snowfall_v2/data/{YYYYMM}/sfav2_CONUS_{24h|48h|72h}_{YYYYMMDD}12.tif`
and season to date
`sfav2_CONUS_{SEASON_START}12_to_{YYYYMMDD}12.tif` (`SEASON_START` is
`20260930` for 2026–27, `20250930` for 2025–26). 6 h grids also exist at 00,
06, 12, and 18Z. Format: GeoTIFF, LZW `float32`, 1500 × 850 pixels of 0.04°,
upper-left corner 126° W, 55° N, nodata `-99999`; values are inches (the
2026-02-07 grid gives 1.00 at Cranmore, matching the North Conway PNS report of
1.0). Read the nearest pixel at each mountain's `lat`/`lon`:
`col = floor((lon + 126) / 0.04)`, `row = floor((55 - lat) / 0.04)` (the
origin and pixel size come from the file's `ModelTiepointTag` and
`ModelPixelScaleTag`). The grid is stored one row per strip, so
`sources/geotiff.py` decodes only the strips holding the eight mountains (a
small stdlib LZW decoder, about 10 ms per grid); no `tifffile`, `imagecodecs`,
or `numpy` dependency. The `.nc` versions are NetCDF4/HDF5 and would need
`h5py`.

Latency: the 24 h grid ending 12Z on day D appeared at about 00Z on D+1
(`sfav2_CONUS_24h_2026112512.tif` last-modified 2026-11-26 00:06Z). It feeds
the 9 PM `forecast-verify` and the next morning's accumulation fallback, not the
same morning. Files are reprocessed later (the 2026-02-07 file was regenerated
2026-02-12), so verification re-reads a grid once after 7 days.

### How `ingest-obs` reads these sources (J12)

Each `ingest-obs` run for report date D:

| Step | Reads | Writes |
|------|-------|--------|
| `pns` | For each office, the api.weather.gov PNS listing (the IEM AFOS listing for D − 1 and D when the live listing fails, or when D is older than yesterday); then each product issued from the start of D − 1 to the end of D (local) that is not stored yet | Text in `discussion` (`kind = 'PNS'`, AFOS product id, so a product is fetched once whichever path found it); reports in `snow_report` |
| `lsr` | IEM LSR GeoJSON for the same window (UTC) | `snow_report` (`hours` null) |
| `nohrsc` | The 24 h, 48 h, and season grids ending 12Z on the newest published date (D − 1 during the day; D after 00:30Z on D + 1, which covers the 9 PM `forecast-verify`), plus the grids ending D − 8 once more if they were first read before the 7-day reprocessing window | One `observation` row per mountain per end date: `source = 'nohrsc'`, `obs_date` = the end date, `reported_at` = 12Z on it, `new_24h_in`, `new_48h_in`, `season_in`; `fetch_id` is the 24 h grid and `raw` holds the pixel, grid URLs, and every grid's `fetch` id. A re-read replaces the rows |

The 72 h grid has no `observation` column and is not fetched. A step fails
(and its section shows "source unavailable") when no office listing could be
read (`pns`), the LSR request fails (`lsr`), or the newest NOHRSC 24 h grid is
neither stored nor fetchable (`nohrsc`); the other steps still run.

`obs_date` convention (all `observation` sources): obs_date D is the 24 h
ending the morning of D. NOHRSC's grid ending 12Z on D (7 AM EST) and a resort
report posted the morning of D both have obs_date D, so the 1d forecast from
report date D (6 AM D to 6 AM D + 1) is verified against obs_date D + 1.

The SNODAS grids named earlier are not used: they are large tar archives, and
the snowfall analysis gives the needed field directly. NOAA's
`mapservices.weather.noaa.gov` NOHRSC service has snow depth and SWE layers
only, no snowfall.

### Open-Meteo historical models

Both APIs take the forecast API's parameters (`latitude`, `longitude`,
`elevation`, `hourly`, `models`, `precipitation_unit=inch`, `timezone`) plus
`start_date` / `end_date`. With `precipitation_unit=inch`, `snowfall` is
inches. Checked at Killington summit for 2025-11-01 to 2026-04-30:

| API | Model | Available | Gaps |
|-----|-------|-----------|------|
| Previous Runs `previous-runs-api.open-meteo.com/v1/forecast` | `gfs_seamless`, `gem_seamless` | `snowfall_previous_day{1..7}`, `precipitation_previous_day{N}`, `temperature_2m_previous_day{N}`, full season | GEM `precipitation_previous_day5`: 39 of 4,344 hours null |
| Previous Runs | `ecmwf_ifs025` | `precipitation_previous_day{N}`, `temperature_2m_previous_day{N}` | `snowfall_previous_day{N}` null for the whole season; `precipitation_previous_day5`: 129 hours null |
| Historical Forecast `historical-forecast-api.open-meteo.com/v1/forecast` | all three | `snowfall`, `precipitation`, `temperature_2m`, 10 m wind and gusts, 850 hPa wind, temperature, humidity | ECMWF `snowfall` null before 2025-11-20T08:00 local; `freezing_level_height` null for ECMWF and GEM |

The Historical Forecast API stitches the first hours of successive runs, so it
measures short-lead output, not the 1d / 3d / 5d forecasts the report scores.
J22 backfills `skill` from Previous Runs at the matching lead
(`previous_day1` for 1d, and so on). ECMWF snowfall at lead is not available,
so the Fallbacks rule applies ("backfill the models and dates it has"): J22
backfills GFS and GEM, and ECMWF `skill` rows start from live verification.

## Excluded

| Source | Why |
|--------|-----|
| OpenSnow, SnowBrains, Powderchasers, OnTheSnow | No free API; terms restrict reuse → would be link-only |
| AmericanWx forums | Unstructured; link-only |
| r/icecoast (incl. its regular forecaster) | Reddit API needs a registered app; posts are copyrighted and irregular. Revisit only with the author's permission |
| Synoptic / MesoWest | Needs a token |
| Liftie (liftie.info API) | Returns 403 behind a Cloudflare bot challenge (2026-11-25). Option: self-host the open-source Liftie on frank |
| NRCS SNOTEL | Too few Northeast sites |
