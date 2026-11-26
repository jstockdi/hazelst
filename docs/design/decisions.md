# Decisions

Newest first. Each entry: the decision, why, and what it replaced.

| # | Date | Decision | Why | Replaces |
|---|------|----------|-----|----------|
| 14 | 2026-11-26 | Morning refresh moves to 8:15 AM (ingest 8:10, waits for ECMWF 06Z until 8:45); verification is scored per edition | 8:15 is the first time every model has a fresh morning run; per-edition scores measure the gain | 8:40 refresh (ingest 8:30) |
| 13 | 2026-11-25 | Report publishes at 7:00 AM (ingest 6:45); refreshes at 8:40 AM and 2:40 PM | Morning GFS (6:17), NWS discussions (6:03–6:41), and resort snow reports land before 7; ECMWF 06Z lands at 8:08 | 5:30 AM build on overnight data |
| 12 | 2026-11-25 | Claude roles and jobs are named `<noun>-<verb>` (`report-write`, `report-revise`, `conditions-extract`, `knowledge-propose`; `report-check` is code) | One readable naming scheme for runs, logs, and `agent_call.role` | Writer / Checker / Extractor / Analyst |
| 11 | 2026-11-25 | All Claude roles default to `claude-opus-5-5` with per-role effort; model is per-role config | Current default model; cheaper models are an owner cost decision | Mixed Sonnet / Haiku / Opus per role |
| 10 | 2026-11-25 | Documentation lives in `docs/` (product, design, operations, experiments) | GitHub Pages is gone, so `docs/` is free for specs | `specs/` |
| 9 | 2026-11-25 | Keep every raw response in SQLite; never prune | Compounding: verification, skill, and knowledge need full history | 30-day snapshot pruning |
| 8 | 2026-11-25 | Host as one container on frank; in-process scheduler; SQLite | frank conventions (digest-pinned compose, Caddy, tunnel); no GitHub Pages | GitHub Actions cron + Pages |
| 7 | 2026-11-25 | Precip type computed in code and checked against prose before publish | Sample run: 3 precip-type errors in the first narrative | Writer inferring type |
| 6 | 2026-11-25 | Headline forecast = model mean; range = per-model min–max | Mean is additive, so chart and table agree | Median |
| 5 | 2026-11-25 | NWS cross-check on 1d and 3d only | NWS snow/ice grids end at ~3 days | 1d/3d/5d |
| 4 | 2026-11-25 | Liftie dropped; lift status from resort adapters | Liftie returns 403 behind a Cloudflare challenge | Liftie API |
| 3 | 2026-11-25 | No link-only sources | Every source must feed the JSON | Linking OpenSnow etc. |
| 2 | 2026-11-25 | Selected UI: morning brief (mockup C), "Hazel St Forecast"; report → forecast → accumulation | Owner review of four mockups | Mockups A, B, D |
| 1 | 2026-11-25 | 1d / 3d / 5d are forecasts from 6 AM; accumulation is separate | Avoid mixing future and past snow | — |
