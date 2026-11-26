# Sunday River fixtures

Boyne site: code-only adapter; never send this content to Claude.

Raw responses captured to design the Sunday River snow-report parser. Files are
gzip-compressed (`gzip -9`) and untrimmed. All requests sent the User-Agent
`hazelst-forecast (github.com/jstockdi/hazelst)`.

| File | Source URL | Final URL | HTTP status | Fetched (UTC) | Trimmed |
| --- | --- | --- | --- | --- | --- |
| `reportpal.json.gz` | https://www.sundayriver.com/api/reportpal?resortName=sr&useReportPal=true | same | 200 | 2026-11-26T14:22:51Z | No |
| `mountain-report.html.gz` | https://www.sundayriver.com/mountain-report | same | 200 | 2026-11-26T14:17:53Z | No |

`reportpal.json.gz` is the parser input. `mountain-report.html.gz` was used only
to discover the resort code (`<meta name="resortCode" content="sr">`) and the
`resortPalQueryParams` site setting; the adapter does not need to fetch it.

## Source notes

- The site is a Next.js app (Sanity CMS) behind Cloudflare. The report page
  renders its stats client-side from the same-origin JSON route
  `/api/reportpal?resortName=<code>&useReportPal=true` (found in the page's
  `/_next/static/chunks/*.js` bundle; a plain GET with no extra headers).
- `https://www.sundayriver.com/robots.txt` (fetched 2026-11-26T14:17:41Z) has `User-agent: *` /
  `Allow: /`, so both `/mountain-report` and `/api/reportpal` are allowed.
  Content-signal line: `Content-Signal: search=yes,ai-train=no,use=reference`.
- No Cloudflare challenge was returned (`cf-cache-status: DYNAMIC`, no
  `cf-mitigated` header).
- Loon, Sunday River, and Sugarloaf share this route and schema; only the host
  and `resortName` (`lm`, `sr`, `sl`) differ.

## Field paths

`LOC` is `.currentConditions.resortLocations.location[0]` (the site's default;
name in this fixture: `"SUNDAY RIVER"`). `RW` is `.currentConditions.resortwide`.

| Field | Path | Value in fixture | Format |
| --- | --- | --- | --- |
| New snow, 24 h | `LOC.snow24Hours.inches` | `"0"` | Inches as a string; `.centimeters` twin |
| New snow, 48 h | `LOC.snow48Hours.inches` | `"0"` | Inches as a string; object may be `null` |
| New snow, 7 days | `LOC.snow7Days.inches` | `"0"` | Inches as a string |
| Base depth | `LOC.base.inches` (also `baseMin`, `baseMax`) | `"0"` | Inches as a string |
| Season total | `LOC.snowSeasonTotal.inches` | `"0"` | Inches as a string |
| Trails open / total | `RW.numTrailsOpen` / `RW.numTrailsTotal` | `3` / `144` | Integers (alpine only) |
| Lifts open / total | `RW.numLiftsOpen` / `RW.numLiftsTotal` | `1` / `19` | Integers |
| Report time | `.updated` | `2026-11-26T14:13:39Z` | ISO-8601 UTC (`Z`) |

`.units` is `"USC"`. Any snow object can be `null` when the resort does not
report it. `.trailsUpdated`, `.liftsUpdated`, and `.commentsUpdated` are also
present. `.operations.resortStatus` is `"CLOSED"` in this fixture.
