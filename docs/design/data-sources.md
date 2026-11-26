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

## Accumulation and conditions

| Source | Endpoint | Fields used | Feeds |
|--------|----------|-------------|-------|
| **Resort mountain reports** | One adapter per resort (report page or its widget JSON) | New 24 h / 48 h / 7-day snow, base depth, season total, trails and lifts open | Accumulation chart, per-mountain notes |
| **NWS snowfall reports** | `api.weather.gov/products/types/PNS/locations/{BTV,ALY,GYX}` | Spotter and CoCoRaHS storm totals | Accumulation cross-check; verification |
| **NOAA NOHRSC** | SNODAS daily grids | 24 h snowfall, snow depth at each mountain's coordinates | Accumulation fallback; verification |
| **Iowa Environmental Mesonet** (to confirm) | LSR / PNS archive (GeoJSON) | Point snowfall reports with coordinates, current and historical | Ground truth; season backfill |
| **Open-Meteo Historical Forecast** (to confirm) | archived model runs | Past forecasts at the same points | Backfill `skill` before the first storm |
| **NWS station observations** | `api.weather.gov/stations/{id}/observations` | Base-area temp, wind, precip type (e.g. KRUT, KMPV, KBML) | "Right now" context |
| **Mount Washington Observatory** | Higher Summits Forecast (text) | Summit wind and temperature, Presidentials | NH/ME wind context |

Lift status comes from the resort adapters (Liftie is blocked; see Excluded).

Resort report adapters are the fragile part: no resort publishes an official
API; on 2026-11-25 five of eight sites returned 200 and three redirected.
Models see only natural snow, so base depth and open terrain exist only in
resort reports. Each adapter is isolated, has a fixture-based test, and on failure the
report falls back to NOHRSC for accumulation and says so in the footer.
Robots and content signals (checked 2026-11-25): Killington, Pico, Sugarbush,
and Cranmore allow general crawling; Stratton publishes no rules. Loon, Sunday
River, and Sugarloaf (Boyne) publish Cloudflare content signals
`search=yes, ai-train=no, use=reference` with no `ai-input` signal — their
adapters are code-only and their page text is never sent to Claude. All
adapters fetch at most 3 times a day with an identifying `User-Agent`.

## Excluded

| Source | Why |
|--------|-----|
| OpenSnow, SnowBrains, Powderchasers, OnTheSnow | No free API; terms restrict reuse → would be link-only |
| AmericanWx forums | Unstructured; link-only |
| r/icecoast (incl. its regular forecaster) | Reddit API needs a registered app; posts are copyrighted and irregular. Revisit only with the author's permission |
| Synoptic / MesoWest | Needs a token |
| Liftie (liftie.info API) | Returns 403 behind a Cloudflare bot challenge (2026-11-25). Option: self-host the open-source Liftie on frank |
| NRCS SNOTEL | Too few Northeast sites |
