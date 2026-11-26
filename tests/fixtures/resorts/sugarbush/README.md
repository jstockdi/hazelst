# Sugarbush fixtures

Raw responses captured to design the Sugarbush snow-report parser. Files are
gzip-compressed (`gzip -9`) and untrimmed. All requests sent the User-Agent
`hazelst-forecast (github.com/jstockdi/hazelst)`.

| File | Source URL | Final URL | HTTP status | Fetched (UTC) | Trimmed |
| --- | --- | --- | --- | --- | --- |
| `report.html.gz` | https://www.sugarbush.com/mountain/conditions | same | 200 | 2026-11-26T14:17:43Z | No |
| `mtnfeed_config.json.gz` | https://v4.mtnfeed.com/resorts/sugarbush.json | same | 200 | 2026-11-26T14:18:31Z | No |
| `snow_api.json.gz` | https://mtnpowder.com/feed/v3.json?bearer_token=<bearerToken from the mtnfeed config>&resortId%5B%5D=70 | same | 200 | 2026-11-26T14:18:47Z | No |

## Source notes

- The report page (Sitecore, Alterra) contains no report data. It sets
  `liftsAndTrailsConfig = { liftsAndTrailsBuilderBasePath: "https://v4.mtnfeed.com/", resortPath: "sugarbush" }`
  and loads the widget `https://v4.mtnfeed.com/static/js/main.js`.
- The widget merges `v4.mtnfeed.com/resorts/default.json` with
  `v4.mtnfeed.com/resorts/sugarbush.json`, then calls
  `https://mtnpowder.com/feed/v3.json?bearer_token=<bearerToken>&resortId[]=<resortIds...>`.
  For Sugarbush, `resortIds` is `[70]`. The bearer token is a public client token
  served in the widget config; resolve it from the config rather than hard-coding it,
  in case it rotates.
- `www.sugarbush.com/robots.txt` allows `/mountain/conditions` (it disallows only
  `/sitecore` and one image). No `Content-Signal` line.
- `v4.mtnfeed.com/robots.txt` allows everything. `mtnpowder.com/robots.txt` disallows
  only `/?resort=*-Summer` pages and the staging host; `/feed/v3.json` is allowed.
  Neither has a `Content-Signal` line.

## Field paths

All paths are relative to `.Resorts[0]`. Snow values are strings; counts are
integers. `--` means no value.

| Field | Path | Value in fixture | Format |
| --- | --- | --- | --- |
| New snow, 24 h | `.SnowReport.AllMountain.Last24HoursIn` (per area: `.SnowReport.{BaseArea,MidMountainArea,SummitArea}.Last24HoursIn`) | `"0"` | Inches, string; `Cm` twin fields |
| New snow, 48 h | `.SnowReport.AllMountain.Last48HoursIn` | `"0"` | Inches, string |
| New snow, 7 days | `.SnowReport.AllMountain.Last7DaysIn` | `"--"` (`Last7DaysCm` is `"0"`) | Inches; may be `--` |
| Base depth | `.SnowReport.<Area>.BaseIn`, or `.SnowReport.SnowBaseRangeIn` | `"0"` / `"--"` | Inches; `PreferredBaseReporting` is `"base"` |
| Season total | `.SnowReport.SeasonTotalIn` | `"7"` | Inches, string |
| Trails open / total | `.SnowReport.TotalOpenTrails` / `.SnowReport.TotalTrails` | `0` / `111` | Integers |
| Lifts open / total | `.SnowReport.TotalOpenLifts` / `.SnowReport.TotalLifts` | `0` / `16` | Integers |
| Snow report time | `.SnowReport.LastUpdate` | `2026-11-25T13:02:17-0500` | ISO-8601 with offset (no colon) |

`.LastUpdate` (feed-level, `2026-11-26T07:15:57-0700`) and `.Resorts[0].LastUpdate`
are feed refresh times, not the snow report time. The narrative report is HTML in
`.SnowReport.Report`. The widget's default area tab for Sugarbush is key `two`, which appears to map to
`SummitArea`.
