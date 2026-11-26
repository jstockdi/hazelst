# NWS PNS fixtures (api.weather.gov)

Live-path responses for Public Information Statements, recorded for J12
(`source-obs`). Request header: `User-Agent: hazelst-forecast (github.com/jstockdi/hazelst)`.
All files are gzip-compressed and untrimmed.

| File | Source URL | HTTP | Fetched (UTC) |
|------|-----------|------|---------------|
| `products_PNS_ALY_list.json.gz` | `https://api.weather.gov/products/types/PNS/locations/ALY` | 200 | 2026-11-26T14:18:26Z |
| `products_PNS_BTV_list_empty.json.gz` | `https://api.weather.gov/products/types/PNS/locations/BTV` (empty `@graph`) | 200 | 2026-11-26T14:18:19Z |
| `product_PNSALY_2026-11-25_non_snow.json.gz` | `https://api.weather.gov/products/89659c91-9eb4-4cec-afcb-f90757e555ae` | 200 | 2026-11-26T14:18:34Z |

The product is a non-snowfall PNS (a radio transmitter notice) with no
`**METADATA**` block; the parser must skip it. Snowfall PNS text samples are
in [`../iem/`](../iem/README.md); their text is identical in both sources.
