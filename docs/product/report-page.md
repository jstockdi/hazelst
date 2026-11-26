# Report Page — Hazel St Forecast

Selected design: `mockups/c-brief.html`. One page per day at
`/d/<date>`; `/` shows today; `/archive` lists days newest first.

## Order, top to bottom

| # | Section | Contents | Source |
|---|---------|----------|--------|
| 0 | **Band** | Kicker "HAZEL ST FORECAST"; headline (Fraunces 18px, one to three lines); issue time (small) | Front matter |
| 1 | **The report** | Best-bet card (mountain + day + one-line why) → The storm → Mountain by mountain → Cautions | Narrative markdown in `report` |
| 2 | **Forecast** | Label "Forecast · what falls next"; (a) stacked bar chart of new snow per mountain by day, y-axis at least 6″; (b) **What falls** grid: mountains × 5 days, each cell the summit `precip_type` (Snow / Ice / Rain / Mix / —) with liquid inches and a 💨 mark on lift-hold wind days; (c) table: Next 24 h / Next 3 days / Next 5 days — mean, model range (hidden when zero-width), NWS value (1d, 3d) | `forecast` in JSON |
| 3 | **Accumulation** | Label "Accumulation · snow that has fallen"; horizontal bars of season total with last 7 days highlighted; base depth at right ("closed" if not open). **Empty state** when no mountain has ≥ 0.5″ in 7 days: one sentence saying so, with the date range | `accumulation` in JSON |
| 4 | **Footer** | Sources used; yesterday's verification (`HIT`/`MISS`) | Fixed text + `verification` |

## Rules

- The headline stays at 18px; no mountain list in the band.
- Forecast and accumulation never share a chart or a table.
- Mountains sort by 5-day forecast in section 2 and by season total in section 3.
- Every forecast number shows its range in the table.
- Charts are inline SVG with fixed viewBox; labels rotate (forecast) or sit
  left-aligned (accumulation) so nothing overlaps at 360 px width.
- Closed mountains (e.g. Pico before opening day) stay listed and say so.
- Precip-type colors: snow `#1a5c88`, ice `#7c3aed`, rain `#12806a`, mix
  `#c4b5fd`, dry `#eef4f8`. Mountain names never wrap in tables.
- Reference implementation: `prototype/render.py` + `prototype/page.css`.

## Numbers-only page

When a report has no checked narrative (`report.status = numbers_only`), the
page keeps every data section and replaces section 1:

- Band headline from the numbers: "Most snow in the next 5 days: <mountain>,
  <n>″ at the summit." (or "No significant snow in the next 5 days." below
  0.5″); the issue line ends "· numbers only".
- A notice card "The written report is delayed."
- Cautions straight from the data: each active NWS alert with its mountains,
  and each `wind_hold_likely` day.

The issue line reads "Thu 26 Nov 2026 · 7:00 AM edition", adding "issued
8:47 AM" when an edition publishes late (ECMWF 06Z gate).

Until ground truth is connected (J14), the accumulation section shows its
empty state: "No snowfall reports are connected yet for the last 7 days
(<range>) …".

Implementation: `render/` (Jinja templates, `charts.py` SVG, `page.css`,
fonts under `render/static/fonts` with their OFL licenses); golden pages in
`tests/golden/`.

## Visual system

Same as the sailing report: Fraunces (display) + Inter (body); navy
`#0f3a5a`, sea blue `#1a5c88`, kelp green `#12806a`, paper `#f5f9fb`. Forecast
chart uses five navy-to-green shades for the five days; accumulation uses light
`#cfe2ef` (earlier) and `#1a5c88` (last 7 days).

## Mobile

Designed at 360–430 px. Max content width 42rem on larger screens. No
horizontal scroll.
