// Sample data for the mockups only. Shape matches DESIGN.md section 4.
window.SKI = {
  issued: "Wed 25 Nov 2026 · 5:30 AM",
  days: ["Thu 26", "Fri 27", "Sat 28", "Sun 29", "Mon 30"],
  headline: "Clipper Thursday, upslope Friday — northern Greens and Sugarloaf win the holiday weekend.",
  bestBet: { key: "sugarbush", day: "Fri 27", why: "NW upslope keeps it snowing all day; light wind, 18°F summit." },
  mountains: [
    { key: "sugarloaf", name: "Sugarloaf", st: "ME", summit: 4237,
      snow: [6, 5, 3, 1, 4], range: { "1d": [4, 8], "3d": [10, 18], "5d": [13, 25] }, conf: ["high", "med", "low"],
      hi: [22, 18, 16, 20, 24], lo: [12, 6, 4, 9, 15], gust: [38, 58, 44, 30, 35], mix: [0, 0, 0, 0, 0],
      reported: { new24: 3, base: 20, trails: [24, 162] }, lifts: [5, 2, 9], score: 84,
      note: "Deepest totals in the region; summit lifts likely on hold Friday (gusts near 58 mph)." },
    { key: "sugarbush", name: "Sugarbush", st: "VT", summit: 3975,
      snow: [5, 4, 3, 0, 3], range: { "1d": [4, 7], "3d": [9, 15], "5d": [11, 19] }, conf: ["high", "med", "med"],
      hi: [24, 18, 17, 22, 26], lo: [14, 9, 7, 11, 16], gust: [28, 30, 26, 20, 31], mix: [0, 0, 0, 0, 0],
      reported: { new24: 2, base: 16, trails: [19, 111] }, lifts: [6, 0, 10], score: 88,
      note: "Upslope favorite Friday. Lincoln Peak quiet on wind — best ski day of the week." },
    { key: "sunday_river", name: "Sunday River", st: "ME", summit: 3140,
      snow: [5, 4, 2, 1, 3], range: { "1d": [3, 7], "3d": [8, 14], "5d": [10, 20] }, conf: ["high", "med", "low"],
      hi: [25, 20, 18, 22, 27], lo: [14, 8, 6, 10, 17], gust: [30, 42, 35, 24, 30], mix: [0, 0, 0, 0, 0],
      reported: { new24: 2, base: 18, trails: [31, 139] }, lifts: [8, 1, 9], score: 79,
      note: "Steady accumulations; most terrain open of any Maine mountain." },
    { key: "killington", name: "Killington", st: "VT", summit: 4241,
      snow: [4, 4, 2, 0, 3], range: { "1d": [3, 6], "3d": [7, 14], "5d": [8, 19] }, conf: ["high", "med", "low"],
      hi: [26, 19, 18, 23, 28], lo: [15, 10, 8, 12, 18], gust: [32, 36, 30, 22, 34], mix: [0, 0, 0, 0, 0],
      reported: { new24: 2, base: 18, trails: [22, 155] }, lifts: [6, 0, 15], score: 76,
      note: "Four inches Thursday, more upslope Friday; K-1 base stays all snow." },
    { key: "pico", name: "Pico", st: "VT", summit: 3967,
      snow: [4, 3, 2, 0, 3], range: { "1d": [3, 5], "3d": [6, 12], "5d": [8, 17] }, conf: ["high", "med", "low"],
      hi: [26, 20, 18, 23, 28], lo: [15, 10, 8, 12, 18], gust: [30, 34, 28, 20, 32], mix: [0, 0, 0, 0, 0],
      reported: { new24: 2, base: 0, trails: [0, 58] }, lifts: [0, 0, 7], score: 0,
      note: "Not open until Dec 11 — snow is banking for opening day." },
    { key: "loon", name: "Loon", st: "NH", summit: 3065,
      snow: [3, 3, 2, 0, 3], range: { "1d": [2, 5], "3d": [6, 11], "5d": [7, 15] }, conf: ["med", "med", "low"],
      hi: [28, 22, 20, 25, 30], lo: [16, 11, 9, 13, 19], gust: [34, 46, 38, 22, 30], mix: [0, 0, 0, 0, 0],
      reported: { new24: 1, base: 14, trails: [18, 72] }, lifts: [4, 1, 6], score: 64,
      note: "Moderate totals; Friday gusts may hold the North Peak lift." },
    { key: "stratton", name: "Stratton", st: "VT", summit: 3875,
      snow: [2, 3, 1, 0, 2], range: { "1d": [1, 4], "3d": [4, 9], "5d": [5, 12] }, conf: ["med", "low", "low"],
      hi: [33, 24, 22, 27, 32], lo: [22, 13, 11, 15, 21], gust: [26, 32, 28, 20, 28], mix: [1, 0, 0, 0, 1],
      reported: { new24: 0, base: 12, trails: [14, 99] }, lifts: [4, 0, 7], score: 52,
      note: "South of the upslope — mixed at the base Thursday, snow above 2,500 ft." },
    { key: "cranmore", name: "Cranmore", st: "NH", summit: 2000,
      snow: [1, 2, 2, 0, 2], range: { "1d": [0, 2], "3d": [3, 8], "5d": [4, 10] }, conf: ["med", "low", "low"],
      hi: [34, 26, 24, 29, 33], lo: [23, 14, 12, 17, 22], gust: [22, 34, 30, 18, 24], mix: [1, 0, 0, 0, 1],
      reported: { new24: 0, base: 10, trails: [9, 57] }, lifts: [2, 0, 5], score: 40,
      note: "Low summit — rain/snow line flirts with the base Thursday and Monday." }
  ]
};
// Observed accumulation (the past): resort-reported new snow for the last 7 days,
// season-to-date total, and current base depth. Separate from the forecast above.
window.SKI.pastDays = ["Wed 18", "Thu 19", "Fri 20", "Sat 21", "Sun 22", "Mon 23", "Tue 24"];
window.SKI.observed = {
  sugarloaf:    { last7: [0, 1, 4, 2, 0, 0, 3], season: 28, base: 20 },
  sugarbush:    { last7: [0, 2, 3, 1, 0, 0, 2], season: 24, base: 16 },
  sunday_river: { last7: [0, 1, 3, 2, 0, 0, 2], season: 22, base: 18 },
  killington:   { last7: [0, 2, 3, 1, 0, 0, 2], season: 25, base: 18 },
  pico:         { last7: [0, 2, 3, 1, 0, 0, 2], season: 23, base: null },
  loon:         { last7: [0, 1, 2, 1, 0, 0, 1], season: 16, base: 14 },
  stratton:     { last7: [0, 1, 2, 0, 0, 0, 0], season: 12, base: 12 },
  cranmore:     { last7: [0, 0, 1, 0, 0, 0, 0], season: 7,  base: 10 }
};
window.SKI.sum =(m, n) => m.snow.slice(0, n).reduce((a, b) => a + b, 0);
