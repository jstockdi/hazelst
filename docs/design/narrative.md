# Narrative

The written report ("Clipper Thursday, upslope Friday — northern Greens and
Sugarloaf win the holiday weekend") is produced by Claude from three inputs:

1. **NWS Area Forecast Discussions** (BTV, ALY, GYX) and **WPC discussions** —
   written by forecasters, they name the system, its track, timing, precip type,
   and confidence. This is what tells us "clipper" vs "coastal" vs "rain-on-ice".
2. **Pattern tags** from `tags.py` — deterministic checks on the model data
   (upslope, mixing, freezing rain, wind holds, storm type).
3. **Per-mountain numbers** — 1d / 3d / 5d forecast, ranges, accumulation.

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
- Precip type words (snow, ice, freezing rain, rain, mix) must match
  `precip_type` for that mountain and day. `report-write` never infers type from
  elevation or temperature on its own.
- Normalize whitespace in discussion text before any keyword match (line wraps
  split phrases).
- Every snow number in prose must match the JSON (summit unless stated).
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

Before publish, `report-check` ([agent-runs.md](agent-runs.md)) validates names,
numbers, precip types, best bet, and cautions against the JSON.
`forecast-verify` scores the 24 h numbers. Weekly, review the MISSes against the tags
that fired and tune thresholds in forecast-method.md.
