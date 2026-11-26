# Mountains

Ikon access for 2026-27 in VT / NH / ME, plus Killington (on Ikon via the
Killington–Pico day allowance; listed explicitly so it is never dropped).

| Key | Mountain | State | Summit ft | Base ft | Lat, Lon | NWS office | Ikon access | Notes |
|-----|----------|-------|-----------|---------|----------|------------|-------------|-------|
| `killington` | Killington | VT | 4,241 | 1,165 | 43.604, -72.820 | BTV | Ikon / Base, days shared with Pico | Always included |
| `pico` | Pico | VT | 3,967 | 2,000 | 43.662, -72.842 | BTV | Shared Killington–Pico days | |
| `sugarbush` | Sugarbush | VT | 3,975 | 1,483 | 44.136, -72.903 | BTV | Full Ikon (Alterra) | NW-flow upslope favored |
| `stratton` | Stratton | VT | 3,875 | 1,872 | 43.113, -72.908 | ALY | Full Ikon (Alterra) | Southern-track storms favored |
| `loon` | Loon | NH | 3,065 | 950 | 44.036, -71.621 | GYX | Ikon (Boyne) | |
| `cranmore` | Cranmore | NH | 2,000 | 500 | 44.056, -71.112 | GYX | 2 days, full Ikon only | Verify 2026-27 status |
| `sunday_river` | Sunday River | ME | 3,140 | 800 | 44.473, -70.857 | GYX | Ikon (Boyne) | |
| `sugarloaf` | Sugarloaf | ME | 4,237 | 1,417 | 45.031, -70.313 | GYX | Ikon (Boyne) | Summit lifts wind-hold often |

Open: confirm whether Pleasant Mountain (ME) or Saddleback (ME) carry Ikon
access for 2026-27. Coordinates and elevations are approximate; confirm before
build.

## mountains.json entry

```json
{
  "key": "killington",
  "name": "Killington",
  "state": "VT",
  "lat": 43.604, "lon": -72.820,
  "summit_ft": 4241, "base_ft": 1165,
  "nws": { "office": "BTV", "grid": "BTV/XX,YY" },
  "report_adapter": "killington",
  "wind_hold_mph": { "watch": 40, "likely": 55 },
  "season_open": "2026-11-22",
  "favored_by": ["nw_upslope", "clipper"]
}
```

`nws.grid` is resolved once from `api.weather.gov/points/{lat},{lon}` and cached.
`favored_by` lists the pattern tags that historically favor this mountain; the
narrative uses it to explain why totals differ (see narrative.md).
