# Report Page — Hazel St Forecast

Selected design: `mockups/c-brief.html`. One page per day at
`/d/<date>`; `/` shows today; `/archive` lists days newest first.

## Order, top to bottom

| # | Section | Contents | Source |
|---|---------|----------|--------|
| 0 | **Band** | Kicker "HAZEL ST FORECAST"; headline (Fraunces 18px, one to three lines); no date or issue line (the date and edition are the last footer line) | Front matter |
| 1 | **The report** | Best-bet card (mountain + day + one-line why) → The storm → Mountain by mountain → Cautions | Narrative markdown in `report` |
| 2 | **Snow timeline** | Label "Snow · reported and forecast", then the grid; no heading or explanatory line. One grid, one row per mountain, one shared date header: name (70 px) │ the **3 days before the report date**, resort-reported only (decisions row 24) │ a 3 px **now rule** │ **4 forecast days** from 6 AM on the report date. Past cells: the resort's number, "0" for a reported zero, a hatched "–" for no report; the past columns always show. Forecast cells (dashed outline): the day's headline forecast (`forecast.days[].snow_in`, so day 1 includes a chosen finer-model `day1_method`); below 0.5″ the precip-type word (rain / ice / mix) if wet, else "·"; otherwise the amount with the type word beneath for rain / ice / mix. Amounts: whole inches from 1″ up, tenths below (".6″"), rounded half up to tenths first (0.97 → 1″). Rows sort by forecast snow over the 4 days, ties in roster order. The now rule is a plain rule; its header cell reads "NOW", set vertically. Each cell has a `title` for desktop hover: past = the resort's exact value and source; forecast = snow, type, liquid, high °F, gusts, lift-hold wind, and the day-1 method. No legend or other explanatory text around the grid beyond the column headers | `accumulation.days` (past), `forecast.days` (future) in JSON |
| 3 | **Forecast** | Label "Forecast · what falls next", then the table (no heading or lead line): Next 24 h / Next 3 days / Next 5 days — headline, model range (hidden when zero-width), NWS value (1d, 3d); under each mountain's name the method behind its numbers ("model mean" or "resort ratio", and "day 1: …" for a finer-model day 1); where it is "resort ratio", "Mean N″" under each value; a note under the table explains both. The method per mountain is the one `method_gate` chose (forecast-method.md, "Method choice"); "Corrected N″" shows under a value only where `method_gate` chose `corrected`. The timeline and the table use the same method per mountain (`forecast.method`), so the days still sum to the windows | `forecast` in JSON |
| 4 | **Accumulation** | Label "Accumulation · snow that has fallen". Resort-reported values only (the resort's live report, else SnoCountry for the same date; decisions row 24). Horizontal bars of season total with last 7 days highlighted, base depth at right ("closed" if not open), for mountains with a resort value. **Empty state** when no mountain has ≥ 0.5″ of resort-reported snow in 7 days: one sentence saying so, with the date range. The non-resort context (NOHRSC, PNS, LSR, GHCN) is not on the page; it stays in the JSON (`days[].context`) and is never summed into the resort value | `accumulation` in JSON (`last7_in`, `season_in`, `base_in`) |
| 5 | **Footer** | Sources used (forecast; accumulation's resort-reported sources; the context sources, as "Not resort-reported, in the JSON only: …"); the newest verification: `HIT` / `MISS` / no-report counts for the model mean's next-24 h forecast, per edition. At 7 AM that is the day before yesterday, since yesterday is scored at 9 PM tonight. Last line, just above the Archive · JSON links: the report date and edition, "Fri 27 Nov 2026 · 2:40 PM" (the issue time only when the report has no edition); no issue time and no "numbers only" | Fixed text + `verification`, `date`, `edition` |

## Rules

- The headline stays at 18px; no mountain list in the band.
- The snow timeline is the one place reported and forecast snow share a grid;
  the now rule separates them. Elsewhere they never share a chart or a table.
- Mountains sort by 4-day forecast in section 2, by 5-day forecast in section
  3, and by season total in section 4.
- Every forecast number shows its range in the table.
- The accumulation chart is inline SVG with a fixed viewBox; labels sit
  left-aligned so nothing overlaps at 360 px width.
- Timeline cells are at least 36 px tall; only the name column (70 px) and the
  now rule (3 px) have fixed widths, so the grid fits 360 px with no
  horizontal scroll. Names may wrap to two lines in the timeline.
- Closed mountains (e.g. Pico before opening day) stay listed and say so.
- Snow amounts, both sides of the now rule (palette D, yellow-green-blue),
  binned on the amount as shown: under 0.5″ white; under 1″ `#f3f8c9`;
  1–3″ `#c7e9b4`; 3–6″ `#7fcdbb`; 6–12″ `#2c7fb8` (white text); 12″+
  `#253494` (white text).
- Precip-type words: rain `#12806a`, ice `#7c3aed`, mix `#a21caf`; on the two
  dark bins the word is white. Mountain names never wrap in tables.
- Reference implementation: `prototype/render.py` + `prototype/page.css`.

## Numbers-only page

When a report has no checked narrative (`report.status = numbers_only`), the
page keeps every data section and replaces section 1:

- Band headline from the numbers: "Most snow in the next 5 days: <mountain>,
  <n>″ at the summit." (or "No significant snow in the next 5 days." below
  0.5″).
- A notice card "The written report is delayed."
- Cautions straight from the data: each active NWS alert with its mountains,
  and each `wind_hold_likely` day.

The footer's date line reads "Thu 26 Nov 2026 · 7:00 AM" even when the
edition publishes late (ECMWF 06Z gate); the issue time stays in the JSON.

When no mountain has a resort-reported value in the 7 days, the
accumulation section shows its empty state: "No resort snow reports for the
last 7 days (<range>)." The timeline still shows its 3 past days, with
"–" where no resort reported.

Implementation: `render/` (Jinja templates, `charts.py` SVG, `page.css`,
fonts under `render/static/fonts` with their OFL licenses); golden pages in
`tests/golden/`.

## Visual system

Same as the sailing report: Fraunces (display) + Inter (body); navy
`#0f3a5a`, sea blue `#1a5c88`, kelp green `#12806a`, paper `#f5f9fb`. The snow
timeline uses palette D (Rules); its now rule is sea blue. Accumulation uses
light `#cfe2ef` (earlier) and `#1a5c88` (last 7 days).

## Mobile

Designed at 360–430 px. Max content width 42rem on larger screens. No
horizontal scroll.
