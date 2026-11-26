# NOHRSC fixtures

National Snowfall Analysis v2 grid recorded for J12 (`source-obs`). Request
header: `User-Agent: hazelst-forecast (github.com/jstockdi/hazelst)`.

| File | Source URL | HTTP | Fetched (UTC) | Trimmed |
|------|-----------|------|---------------|---------|
| `sfav2_CONUS_24h_2026020712.tif.gz` | `https://www.nohrsc.noaa.gov/snowfall_v2/data/202602/sfav2_CONUS_24h_2026020712.tif` | 200 | 2026-11-26T14:20:11Z | No (519 KB raw, 339 KB gzip) |

Grid: GeoTIFF, LZW-compressed `float32`, 1500 × 850, 0.04° pixels,
upper-left corner at 126° W, 55° N (`ModelTiepointTag`), nodata `-99999`.
Values are 24 h snowfall ending 2026-02-07 12Z, in inches. Nearest-pixel values
at the mountain coordinates in `prototype/mountains.json`:

| Mountain | Value (in) |
|----------|-----------|
| killington | 3.17 |
| pico | 2.55 |
| sugarbush | 1.90 |
| stratton | 2.14 |
| loon | 1.88 |
| cranmore | 1.00 |
| sunday_river | 0.49 |
| sugarloaf | 0.15 |

A normalize test can assert these values.
