# Open-Meteo historical fixtures

Responses recorded for J22 (`season-backfill`) at Killington's coordinates with
`elevation=1293` (summit, meters). Request header:
`User-Agent: hazelst-forecast (github.com/jstockdi/hazelst)`. All files are
gzip-compressed and untrimmed.

| File | Source URL | HTTP | Fetched (UTC) |
|------|-----------|------|---------------|
| `historical_forecast_killington_2026-02-05_2026-02-07.json.gz` | `https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=43.604&longitude=-72.820&elevation=1293&start_date=2026-02-05&end_date=2026-02-07&hourly=snowfall,precipitation,temperature_2m,wind_speed_10m,wind_gusts_10m,freezing_level_height,wind_direction_850hPa,wind_speed_850hPa,temperature_850hPa,relative_humidity_850hPa&models=gfs_seamless,ecmwf_ifs025,gem_seamless&precipitation_unit=inch&timezone=America/New_York` | 200 | 2026-11-26T14:21:35Z |
| `historical_forecast_killington_season_2025-26.json.gz` | `https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=43.604&longitude=-72.820&elevation=1293&start_date=2025-11-01&end_date=2026-04-30&hourly=snowfall,precipitation,temperature_2m&models=gfs_seamless,ecmwf_ifs025,gem_seamless&precipitation_unit=inch&timezone=America/New_York` | 200 | 2026-11-26T14:22:05Z |
| `previous_runs_killington_2026-02-05_2026-02-07.json.gz` | `https://previous-runs-api.open-meteo.com/v1/forecast?latitude=43.604&longitude=-72.820&elevation=1293&start_date=2026-02-05&end_date=2026-02-07&hourly=snowfall,snowfall_previous_day1,snowfall_previous_day3,snowfall_previous_day5,temperature_2m_previous_day1&models=gfs_seamless,ecmwf_ifs025,gem_seamless&precipitation_unit=inch&timezone=America/New_York` | 200 | 2026-11-26T14:22:20Z |
| `previous_runs_killington_season_2025-26.json.gz` | `https://previous-runs-api.open-meteo.com/v1/forecast?latitude=43.604&longitude=-72.820&elevation=1293&start_date=2025-11-01&end_date=2026-04-30&hourly=snowfall_previous_day1,snowfall_previous_day5,precipitation_previous_day1,precipitation_previous_day5&models=gfs_seamless,ecmwf_ifs025,gem_seamless&precipitation_unit=inch&timezone=America/New_York` | 200 | 2026-11-26T14:22:33Z |

Coverage seen in these files (non-null hours):

- Historical Forecast, season file: GFS and GEM complete; ECMWF `snowfall`
  null before 2025-11-20T08:00 local (464 of 4,344 hours), ECMWF
  `precipitation` complete.
- Historical Forecast, storm file: `freezing_level_height` null for ECMWF and
  GEM; every other variable complete for all three models.
- Previous Runs, season file: `snowfall_previous_dayN` null for ECMWF in every
  hour; ECMWF and GEM `precipitation_previous_day5` miss 129 and 39 hours.
