# Cranmore fixtures

Resort key: `cranmore` (Cranmore Mountain Resort, North Conway, NH).

The snow report is server-rendered HTML (Craft CMS). The page loads no JSON
endpoint or third-party snow widget, and it embeds no JSON data, so the parser
reads HTML selectors. robots.txt (`https://cranmore.com/robots.txt`) disallows
only `/cpresources/`; the report path is allowed. No `Content-Signal` line is
present.

All files are gzip-compressed (`gzip -9`) and untrimmed.

| File | Source URL | Final URL | HTTP status | Fetched (UTC) | Trimmed |
|------|------------|-----------|-------------|---------------|---------|
| `snow-report.html.gz` | `https://cranmore.com/snow-report` | `https://cranmore.com/snow-report` | 200 | 2026-11-26T14:18:28Z | No |
| `snow-report-2026-02-11-archived.html.gz` | `https://web.archive.org/web/20260215000000id_/https://cranmore.com/snow-report` | `https://web.archive.org/web/20260212004122id_/https://cranmore.com/Snow-Report` | 200 | 2026-11-26T14:19:05Z | No |
| `homepage.html.gz` | `https://cranmore.com/` | `https://cranmore.com/` | 200 | 2026-11-26T14:18:15Z | No |

## Notes

- `snow-report.html.gz` is a pre-season capture (report dated Nov 24, 2026).
  Lifts and trails are all closed, and the snowfall and base-depth datapoints
  are omitted from the markup entirely.
- `snow-report-2026-02-11-archived.html.gz` is an Internet Archive capture of
  the same page in season (report dated Feb 11, 2026). It shows the full set
  of datapoints: Base Depth, Overnight, 7 Day Total and Season Total. The `id_`
  modifier returns the original markup without archive rewriting.
- `homepage.html.gz` contains the site-wide report bar (`.reportbar`), which
  shows a single Snowfall value and the current temperature. It is a secondary
  source only.
