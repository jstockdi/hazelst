# Narrative

The written report ("Clipper Thursday, upslope Friday — northern Greens and
Sugarloaf win the holiday weekend") is produced by Claude from three inputs:

1. **NWS Area Forecast Discussions** (BTV, ALY, GYX) and **WPC discussions** —
   written by forecasters, they name the system, its track, timing, precip type,
   and confidence. This is what tells us "clipper" vs "coastal" vs "rain-on-ice".
2. **Pattern tags** from `tags.py` — deterministic checks on the model data
   (upslope, mixing, freezing rain, wind holds, storm type).
3. **Per-mountain numbers** — 1d / 3d / 5d forecast, ranges, accumulation.
   The digest's accumulation is what each resort reported (`last7_in`,
   `season_in`, `base_in`; `null` = no report). Other sources for the same
   days (NOHRSC at its pixel, then the three highest PNS / LSR / GHCN reports
   within 20 mi per day, each with place, elevation, and distance) travel in
   `accumulation.not_resort_reported`, labelled as not resort-reported
   (decisions row 24).

## Feasibility check (2026-11-25)

The BTV discussion issued 6:07 AM EST on 2026-11-25 stated, among other things:
a low "will travel northward through the eastern United States"; freezing rain
"between 8 PM and 4 AM Thursday night"; an "easterly 70 to 80 knot low level
jet" with stronger winds above 2,000 ft in the southern Greens; transition to
rain Friday. That is enough to write the storm section and the cautions from
data — and it shows the mockup's clipper scenario is sample data, not this
week's weather. The full sample run is in [the sample run](../experiments/2026-11-25-sample-run.md).

## Rules (go into SKILL.md)

- Name a storm type only if `storm_type` or a discussion names it. Otherwise
  describe what happens ("snow Thursday, drying Sunday").
- Precip type words (snow, ice, freezing rain, rain, mix) must match the
  mountain-day's type at the elevation the phrase names: `precip_type`
  (summit) or `base_precip_type` (base); freezing rain matches where
  `freezing_rain_at` lists it. `report-write` never infers type from
  elevation or temperature on its own.
- The headline is a text alert, at most 12 words: amount, resorts by name,
  day; then the one catch ("4–5″ for Killington, Pico and Sugarbush Saturday;
  rain Monday"). No colon setups, no "X is the day", no regions in place of
  resort names, no adjectives.
- Normalize whitespace in discussion text before any keyword match (line wraps
  split phrases).
- Every snow number in prose must match the JSON (summit unless stated).
- Observed snow at a mountain is the resort's own number. A non-resort value
  (`not_resort_reported`) is never presented as the mountain's snowfall or
  added to it; when cited, it names its source, place, and elevation ("the
  Stowe co-op at 720 ft had 3″").
- When `nws_disagrees` is set, say which source the report leans on and why.
- When models spread wide (low confidence), say so in the storm section.
- Explain mountain-to-mountain differences with tags and `favored_by`
  (e.g. upslope favors Sugarbush and Killington; coastal favors Maine).
- Cautions: wind holds, mixing, freezing rain, active NWS alerts — and nothing
  else.
- Credit forecaster input as "NWS Burlington", "NWS Gray", "NWS Albany", "WPC".

## Regional knowledge (seed for SKILL.md)

- **NW-flow upslope** after a front loads the northern Greens (Sugarbush, then
  Killington/Pico) past model totals; Stratton and the Whites see little.
- **Coastal storms / nor'easters** favor Maine and NH (Sunday River, Sugarloaf)
  and southern VT (Stratton) on a southern track; watch the mixing line.
- **Inland runners** (low tracking west of the mountains) bring warm air and rain
  to all; summits may hold snow or ice.
- **Sugarloaf summit** is above treeline — summit lifts hold first on wind.
- **Valley rain, summit snow** is common early and late season.

## Output structure

Claude returns a `ReportDraft` (agent-runs.md). The page shows, in order: the
headline (band), Best bet (card), then The storm → Mountain by mountain →
Cautions. `report.headline` and `report.best_bet` hold the first two;
`report.narrative_md` holds the three sections as markdown (`## ` headings,
paragraphs, `**bold**`). Charts and tables are added by the page templates,
not written by Claude.

## Quality loop

Before publish, `report-check` ([agent-runs.md](agent-runs.md)) validates the
headline, names, numbers, precip types, best bet, and cautions against the
JSON. A draft that fails gets up to three `report-revise` rounds; if it still
fails, nothing is published and the job fails (decisions row 33).
`forecast-verify` scores the 24 h numbers. Weekly, review the MISSes against the tags
that fired and tune thresholds in forecast-method.md.
