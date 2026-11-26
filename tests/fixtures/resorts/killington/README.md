# Killington fixtures

Raw responses captured to design the Killington snow-report parser. Files are
gzip-compressed (`gzip -9`) and untrimmed. All requests sent the User-Agent
`hazelst-forecast (github.com/jstockdi/hazelst)`.

| File | Source URL | Final URL | HTTP status | Fetched (UTC) | Trimmed |
| --- | --- | --- | --- | --- | --- |
| `report.html.gz` | https://www.killington.com/the-mountain/conditions-weather/current-conditions-weather | https://killington.com/mountain-report | 200 | 2026-11-26T14:17:27Z | No |
| `robots.txt.gz` | https://www.killington.com/robots.txt | https://killington.com/robots.txt | 200 | 2026-11-26T14:17:18Z | No |

## Source notes

- The site runs on MODX and renders the report server-side. No JSON endpoint was
  found in the page or in its script bundle, and the page embeds no report state
  as JSON. The parser reads HTML.
- `robots.txt` allows `/mountain-report` for `User-agent: *`. It disallows
  `/dor/` among other CMS paths. It has no `Content-Signal` line.
- Scope all selectors to `main#page`. The header menu (`.drop[data-drop="menu-alt"]`)
  repeats the snow grid and status bars.

## Selectors (within `main#page`)

| Field | Selector | Value in fixture | Format |
| --- | --- | --- | --- |
| New snow, 24 h | `.dor-snow-report .dor-snow-cell` whose `.dor-snow-label` is `24 Hour`, then `.dor-snow-val` | `0"` | Inches, `"` suffix |
| New snow, 48 h | same, label `48 Hour` | `0"` | Inches |
| New snow, 7 days | same, label `7 Day` | `0"` | Inches |
| Season total | same, label `Season` | `5"` | Inches |
| Base depth | Not published | | |
| Lifts open / total | `section.dor-status-bars-wrap a.dor-bar-row[aria-label="Open Lifts"] .dor-bar-count` | `2/19` | `open/total` |
| Trails open / total | `section.dor-status-bars-wrap a.dor-bar-row[aria-label="Open Trails"] .dor-bar-count` | `3/155` | `open/total` |
| Snow report time | `.dor-snow-report > .condition-report-date` | `Updated November 15, 2026 10:44 AM` | Strip `Updated `; US Eastern local time, no zone |

The narrative daily report carries its own time in
`section.condition-report .condition-report-date` (`November 26, 2026 8:32 AM`
in this fixture).

`https://killington.com/mountain-overview/`, the target of the snow-grid links,
was also checked for a base depth. It had no report data at fetch time and is not
stored.
