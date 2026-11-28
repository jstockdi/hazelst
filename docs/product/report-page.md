# Report Page — Hazel St Forecast

Selected design: J35 overhaul (option "ab": spread in the cell, aligned model
drawer), replacing mockup C. One page per day at `/d/<date>`; `/` shows
today; `/archive` lists days newest first.

## Order, top to bottom

| # | Section | Contents | Source |
|---|---------|----------|--------|
| 0 | **Header** | Small "HAZEL ST" wordmark (accent); the headline in Fraunces 22px; no date or issue line | Front matter |
| 1 | **Snow grid** | One white card, no heading. Columns: name │ the **3 days before the report date**, resort-reported only (decisions row 24) │ a **now rule** │ **4 forecast days** from 6 AM on the report date, on a tinted background. One shared date header; one 44 px row per mountain; rows sort by forecast snow over the 4 days, ties in roster order. Cells below | `accumulation.days` (past), `forecast.days` (future) |
| 2 | **Model drawer** | Tap a mountain name (a button with a chevron) to open a full-width row under it; closed by default, one tap per drawer, no hover. Rows: **Hazel St** (bold, the headline values), GFS, ECMWF, GEM, then each finer model with a value on a shown day, then **NWS** (the 1-day value under day 1; the 3-day value across days 2–3 as "3″ to Sun"). The drawer uses CSS subgrid, so each value sits under its day column and there is no second date header; the model name spans the name and past columns. Nothing under the NWS row (J36 removed the method line) | `forecast.days[].by_model`, `windows.1d/3d.nws_in` |
| 3 | **The report** | A second card after the grid: best bet (mountain + day + why) → The storm → Mountain by mountain → Cautions, with only those section names as headings | Narrative markdown in `report` |
| 4 | **Footer** | One centered line: "Fri 27 Nov 2026 · 2:40 PM · Archive · JSON" (the issue time only when the report has no edition) | `date`, `edition` |

There is no forecast table and no method note; windows, ranges, NWS,
sources, and verification stay in the JSON.

## Grid cells

- **Forecast, ≥ 0.5″:** a filled rounded cell in palette D with the amount.
  Under it, small: the global models' range ("2–8", no inch marks) when they
  disagree, else the precip word for rain / ice / mix.
- **Disagree:** over the models in `used_models` that have a value for the
  day, max − min ≥ max(1.5″, half the day's amount). The range is whole
  inches, rounded half up.
- **Forecast, < 0.5″:** the precip word (rain / ice / mix) in its ink colour,
  else a faint "0".
- **Past:** the resort's number (filled at ≥ 0.5″), a faint "0" for a
  reported amount under 0.5″, blank for no report. The past columns always
  show.
- **Amounts:** whole inches from 1″, tenths below (".6″"), rounded half up to
  tenths first (0.97 → 1″). Drawer values use the same rule, "0" below 0.5″,
  "·" for no value.
- Each grid cell keeps a `title` for desktop hover: past = the resort's
  exact value and source; forecast = snow, type, liquid, high °F, gusts,
  lift-hold wind, and the day-1 method.

## Rules

- Theme: page `#f4f6f8`; white cards with a 1px `#e3e9ee` border, radius 14,
  no shadows, no gradient band. Tokens on `:root`: ink `#132534`, mute
  `#64788a`, faint `#b8c4ce`, line `#e3e9ee`, accent `#1a5c88`, forecast
  tint `#f0f6fb`.
- Type: Fraunces for the headline only; Inter elsewhere, in three sizes
  (15 / 13 / 12 px; the in-cell range is 11 px).
- Grid columns: `minmax(88px,1.4fr) repeat(3,1fr) 10px repeat(4,1fr)`; the
  grid fits 360 px with no horizontal scroll. Names may wrap to two lines.
- Snow amounts, both sides of the now rule (palette D, yellow-green-blue),
  binned on the amount as shown: under 0.5″ not filled; under 1″ `#f3f8c9`;
  1–3″ `#c7e9b4`; 3–6″ `#7fcdbb`; 6–12″ `#2c7fb8` (white text); 12″+
  `#253494` (white text).
- Precip words on unfilled cells: rain `#0f6b57`, ice and mix `#6d28d9`; on a
  filled cell the word takes the cell's text colour.
- The model list is data-driven: the drawer shows every model in the day's
  `by_model`, once each, under its display name, in `MODEL_NAMES` order:
  GFS, ECMWF, GEM, ECMWF 9 km, HRRR, HRDPS, NBM, AIFS (J36). Payloads stored
  before J34 carry `gfs_seamless` / `gem_seamless` as their GFS and GEM
  series and show under those names. A key with no display name fails the
  render (`UnknownModelError`), and `report-build` / `report-refresh` render
  the timeline before publishing, so the job fails instead of printing an
  Open-Meteo id. The spread uses `used_models`.
- Closed mountains stay listed.
- The name column fits every mountain name on one line at 360 px, with a gap
  before the chevron.
- Payloads stored before J35 have no `by_model`: no in-cell range, and the
  drawer shows Hazel St and NWS only.

## Every page has the written report

There is no page without prose (J33, decisions row 33). An edition is
published only when its narrative passes `report-check`; when writing fails
(a Claude call fails, or the checks still fail after three revisions), the
job fails, an alert fires, and the page keeps showing the previous edition
until the failure is fixed. Rows stored before J33 with
`report.status = numbers_only` stay in the database for history and are never
shown.

The header headline is a ski forecaster's text alert, at most 12 words: the
amount, the resorts by name, the day; then the one catch that changes plans
("4–5″ for Killington, Pico and Sugarbush Saturday; rain Monday"; "No new snow
through Tuesday at any of the eight"). `report-check` holds it to that length,
to naming a resort (or "all eight"), and to forecast days.

The footer reads "Thu 26 Nov 2026 · 7:00 AM · Archive · JSON" even when the
edition publishes late (ECMWF 06Z gate); the issue time stays in the JSON.

When no resort reported in the last 3 days, the grid still shows its 3 past
days, blank.

Implementation: `render/` (Jinja templates, `page.css`, the drawer toggle inline in `report.html`,
fonts under `render/static/fonts` with their OFL licenses); golden pages in
`tests/golden/`.

## Mobile

Designed at 360–430 px, checked at 728 px. Max content width 44rem on larger
screens. No horizontal scroll.
