---
name: hazel-st
description: Regional knowledge and writing rules for the Hazel St Forecast ski report (Ikon mountains of Vermont, New Hampshire, and Maine). Use when writing, revising, or reviewing a Hazel St Forecast report or explaining why mountains differ.
---

# Hazel St Forecast: regional knowledge and rules

The report covers eight mountains: Killington, Pico, Sugarbush, and Stratton
(Vermont); Loon and Cranmore (New Hampshire); Sunday River and Sugarloaf
(Maine). Forecasters: NWS Burlington (BTV) covers Killington, Pico, and
Sugarbush; NWS Albany (ALY) covers Stratton; NWS Gray (GYX) covers Loon,
Cranmore, Sunday River, and Sugarloaf.

## Rules

1. **Numbers come from the digest.** Every snow amount in prose is a value in
   the digest for that mountain (a day's `snow_in`, a window's mean or range,
   or the NWS value), within half an inch. Write inches with `″` ("3″",
   "2–4″"). Snow amounts are at the summit unless the sentence says "base".
2. **Precip type comes from the digest, by elevation.** For each mountain and
   day, the words snow, ice, freezing rain, rain, mix, and dry must match
   `precip_type` (summit) or, in a phrase that names the base,
   `base_precip_type`. Freezing rain is right wherever `freezing_rain_at` lists
   it, even on a `rain` day. Never infer type from temperature, elevation, or a
   storm's track. `ice` days are freezing rain or glaze, never "rain" or
   "mix". A `mix` day may be described as snow and rain mixed. Describe base
   conditions in their own phrase that names the base ("rain at the base").
   Give each mountain list its own type ("freezing rain at Killington and
   Pico, with rain at Stratton"). Do not name a precip type that does not
   happen that day, even in passing ("holds on to ice longer than rain" names
   rain).
3. **Days by name.** Refer to days by weekday name ("Thursday", "Thursday
   night") or today, tonight, tomorrow. A day runs 6 AM to 6 AM, so
   "Thursday night" belongs to Thursday.
4. **Storm type only when named.** Say clipper, coastal storm, nor'easter, or
   inland runner only when a mountain's `storm_type` or a discussion's
   `storm_words` names it, and name at most one per report. Say "upslope"
   only when a day carries the `nw_upslope` tag or a discussion names
   upslope. Otherwise describe what happens ("snow Thursday, drying Sunday").
   A track never decides snow vs rain: a coastal low on 2026-11-25 brought ice
   and rain.
5. **Disagreement and confidence.** When a window has `nws_disagrees`, give
   the NWS number once, in plain words ("though NWS Burlington says 1″", "NWS
   has a third of that"). Never explain the report's method, the models, or
   which numbers it uses. When `confidence` is low (models spread more than
   6″), say so plainly in the storm section and give the range.
6. **Explain differences** between mountains with tags and `favored_by`, not
   with general claims.
7. **Cautions** list wind holds, mixing, freezing rain, and every active NWS
   alert by its exact event name, and nothing else: one item per day, merging
   that day's hazards. Each `wind_hold_likely` day names the mountain, the
   day, and the wind hold.
8. **Credit forecasters** only when they disagree with the report or add
   something it lacks, once each, as "NWS Burlington", "NWS Albany", "NWS
   Gray", or "WPC".
9. **Best bet**, when a day is worth chasing, is a mountain that is open that
   day (`season_open` on or before it) and a day in the digest. When none is
   (only mixed or wet snow under about 2″, rain everywhere, or nothing open),
   there is no best bet. A `null` `season_open` means the opening date is not
   known: do not say the mountain is closed or not open.

## Regional patterns

- **NW-flow upslope.** After a cold front, northwest winds lift moist air up
  the western slopes of the Green Mountains. The northern Greens load first and
  most: Sugarbush, then Killington and Pico, often past model totals. Stratton
  and the White Mountains (Loon, Cranmore) see little. Tag: `nw_upslope`
  (850 hPa wind 270–340°, at least 20 kt, RH at least 75%, at most -8 °C, for
  6 hours or more).
- **Coastal storms and nor'easters.** Lows tracking up the coast favor Maine
  and New Hampshire (Sunday River, Sugarloaf) and, on a southern track,
  southern Vermont (Stratton). The rain-snow line runs close to the coast and
  can push inland; watch the mixing line.
- **Inland runners** (lows tracking west of the mountains) bring warm air and
  rain to all eight; summits may hold snow or ice longer than the bases.
- **Clippers.** Fast, moisture-starved lows from Canada: light, dry snow,
  often enhanced by upslope in the northern Greens behind them.
- **Mixing at the base.** Bases sit 500–2,000 ft; summits 2,000–4,240 ft.
  Early and late season, valley rain with summit snow is common:
  `base_precip_type` is `rain` or `mix` while `precip_type` is `snow`.
  Cranmore's summit (2,000 ft) is barely above other mountains' bases.
- **Freezing rain.** Warm air aloft over cold air trapped in the valleys and
  on the summits: 850 hPa above freezing while the surface is at or below
  freezing. `freezing_rain_at` lists the elevations where it falls (set at
  both when NWS forecasts ice accumulation). It glazes trails and lifts and
  makes mountain roads dangerous.
- **Wind holds.** Summit lifts stop in high wind. Thresholds per mountain:
  watch at 40 mph gusts (35 at Loon and Sugarloaf), likely at 55 (50 at Loon
  and Sugarloaf). Sugarloaf's summit is above treeline, so its summit lifts
  hold first. Tags: `wind_hold_watch`, `wind_hold_likely`.
- **Early season.** Snowmaking bases do not show in model snow depth; resort
  reports carry the base. Pico opens later than Killington.

## Powder score

A 0–100 ranking used to order mountains: 24-hour summit snow (50%), summit
temperature at or below 20 °F (20%), gusts below the wind-hold watch threshold
(20%), and no mixing (10%). The best bet usually has the highest powder score
for its day.
