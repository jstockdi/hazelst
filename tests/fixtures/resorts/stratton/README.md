# Stratton fixtures

Raw responses captured to design the Stratton snow-report parser. Files are
gzip-compressed (`gzip -9`) and untrimmed. All requests sent the User-Agent
`hazelst-forecast (github.com/jstockdi/hazelst)`.

| File | Source URL | Final URL | HTTP status | Fetched (UTC) | Trimmed |
| --- | --- | --- | --- | --- | --- |
| `report_blocked.html.gz` | https://www.stratton.com/the-mountain/mountain-report | same | 200 | 2026-11-26T14:19:11Z | No |
| `mtnfeed_config.json.gz` | https://v4.mtnfeed.com/resorts/stratton.json | same | 200 | 2026-11-26T14:18:31Z | No |
| `snow_api.json.gz` | https://mtnpowder.com/feed/v3.json?bearer_token=<bearerToken from the mtnfeed config>&resortId%5B%5D=1 | same | 200 | 2026-11-26T14:19:27Z | No |

## Source notes

- `www.stratton.com` is behind Imperva (Incapsula) bot protection. Both
  `/robots.txt` and the report page returned HTTP 200 with an Incapsula challenge
  page (an iframe to `/_Incapsula_Resource`) instead of content.
  `report_blocked.html.gz` is that challenge page; keep it to test block detection.
  The robots.txt verdict and `Content-Signal` line for `www.stratton.com` are
  therefore unknown.
- Stratton uses the same Alterra widget as Sugarbush. `v4.mtnfeed.com/resorts/stratton.json`
  gives `resortIds: [1]` and its own bearer token, and the feed returns
  `"Name": "Stratton"`. The data is read from `mtnpowder.com`, not from
  `www.stratton.com`, so the challenge does not block the adapter.
- `v4.mtnfeed.com/robots.txt` allows everything. `mtnpowder.com/robots.txt` allows
  `/feed/v3.json` (it disallows `/?resort=Stratton-Summer` and other summer pages).
  Neither has a `Content-Signal` line.

## Field paths

Same schema as Sugarbush. All paths are relative to `.Resorts[0]`.

| Field | Path | Value in fixture | Format |
| --- | --- | --- | --- |
| New snow, 24 h | `.SnowReport.AllMountain.Last24HoursIn` | `"0"` | Inches, string; `Cm` twin fields |
| New snow, 48 h | `.SnowReport.AllMountain.Last48HoursIn` | `"0"` | Inches, string |
| New snow, 7 days | `.SnowReport.AllMountain.Last7DaysIn` | `"0"` | Inches, string |
| Base depth | `.SnowReport.<Area>.BaseIn`, or `.SnowReport.SnowBaseRangeIn` | `"0"` / `"--"` | Inches; `PreferredBaseReporting` is `"summit"` |
| Season total | `.SnowReport.SeasonTotalIn` | `"6"` | Inches, string |
| Trails open / total | `.SnowReport.TotalOpenTrails` / `.SnowReport.TotalTrails` | `0` / `99` | Integers |
| Lifts open / total | `.SnowReport.TotalOpenLifts` / `.SnowReport.TotalLifts` | `0` / `14` | Integers |
| Snow report time | `.SnowReport.LastUpdate` | `2026-11-25T16:04:24-0500` | ISO-8601 with offset (no colon) |

`.Resorts[0].OperatingStatus` is `"Closed"` in this fixture. The widget's default
area tab for Stratton is key `zero`, which appears to map to `BaseArea`.
