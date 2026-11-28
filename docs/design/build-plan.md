# Build Plan

The build from an empty repo to production, as a series of jobs. Each job is
one branch and one PR to `main`, merged only with CI green, and leaves the app
runnable. Milestones M1–M7 are defined in
[technical-design.md §16](technical-design.md#16-milestones-to-production).

Jobs run as unattended threads. Everything that needs the owner is resolved in
[Pre-flight](#pre-flight) before the first thread starts; inside a job, the
[thread rules](#thread-rules) and [fallbacks](#fallbacks) decide instead of
asking.

## Pre-flight

| # | Item | Status |
|---|------|--------|
| P1 | Claude token: owner runs `claude setup-token`; the token arrives through the secret prompt and is stored at `~/.config/hazelst/claude.env` (`CLAUDE_CODE_OAUTH_TOKEN=…`, mode 600, outside the repo). Threads source it for live calls; J17 SOPS-encrypts it for frank | Verified 2026-11-26: stored; one CLI call with a clean `HOME` succeeded |
| P2 | GHCR: an image pushed by Actions from this public repo is public with no extra step | Verified 2026-11-26 (probe tag `probe`; package visibility `public`, anonymous pull token issued). J04 deletes the `probe` version after the first real push |
| P3 | frank checkout: `/opt/frank` is on branch `ordinance-member-stack`, 14 commits ahead of `origin/main`, no PR open. `ordinance-member-stack` merges to `main` first; then J17 opens the hazelst frank PR against `main`, and J18 switches `/opt/frank` to `main` (which then contains both) before deploying | Done 2026-11-26: merged as frank `7762922` (!66, commits re-signed for the signed-commit rule); `main` tree equals `447b8c0`. `/opt/frank` has one uncommitted comment edit in `projects/ordinance-member/compose.yml` that J18 keeps |
| P4 | Unattended approvals: (a) each thread squash-merges its own hazelst PR when CI is green; (b) J17 merges the frank PR; (c) J18 host bundle (already approved); (d) J19 ingress change, which restarts the shared platform stack (`play.`, `bcl.` blip for seconds) | Approved 2026-11-26 (all four) |
| P5 | Access and tools: PyPI and GitHub reachable; `ssh frank` with passwordless `sudo`; frank is `x86_64` with Docker 29.5, `sops`, `age`; no `sqlite3` CLI on frank, so backups use Python's `sqlite3.backup` inside the container | Verified 2026-11-26 |
| P6 | Claude usage counts against the owner's subscription (no API workspace cap). The in-app daily call cap (config, default 10 calls) is the limit | Noted |

## Thread rules

Every job thread:

1. Reads `docs/README.md`, `docs/design/technical-design.md`,
   `docs/design/decisions.md`, and the docs its job names.
2. Works in its own worktree off `origin/main` on branch `job/<id>-<name>`
   (for example `job/J05-source-open-meteo`).
3. Rebases on `origin/main` before merging; opens a PR; squash-merges when CI is
   green; deletes the branch.
4. Updates the spec in the same PR when reality differs from it, and adds a row
   to `decisions.md` when that reverses a choice. Picks the simplest option
   consistent with the docs; never waits for input outside the owner gates in
   [Owner actions](#owner-actions).
5. Never commits secrets or personal data. The NWS `User-Agent` is
   `hazelst-forecast (github.com/jstockdi/hazelst)`.
6. Touches frank only in J17–J20.
7. Schema: J03 creates every table in data-model.md. A later job that changes
   the schema adds the next numbered migration after rebasing.
8. Ends with: PR URL, merge SHA, and the output that shows each "Done when" item.

## Fallbacks

| Unknown | Decision without asking |
|---------|------------------------|
| Agent SDK has no structured-output option | Put the JSON schema in the prompt, validate with Pydantic, one retry with the validation error; record in `decisions.md` |
| Agent SDK cannot run on a read-only rootfs | Set `HOME` to a tmpfs path (`/tmp/home`); if that fails, a writable `/data/claude-home` |
| IEM or NOHRSC endpoint unavailable or changed | Drop that source, show "source unavailable", record in `data-sources.md` |
| A resort page is blocked or has no parseable data | Disable that adapter; that mountain uses SnoCountry's copy of the resort number, else `NO_OBS` / "no report" (PNS / LSR / GHCN / NOHRSC stay context, decisions row 24); record in `data-sources.md` |
| ECMWF 06Z not available by 8:45 | The 8:15 edition publishes with the 00Z run and says so in the footer |
| Open-Meteo Historical Forecast lacks a model or season | Backfill the models and dates it has |
| Open-Meteo Previous Runs lacks the 850 hPa variables for a model | Fit J27 ratios on summit temperature only for that model; record in `data-sources.md` |
| No snow falls during J14 | "Done when" is met on fixtures; the live check is deferred to the first snow day and noted in the PR |
| A dependency release is under two weeks old | Pin the previous release |

## Sequence

```
M1  J01 ─▶ J02 ─▶ J03 ─▶ J04
                    │
M2                  ├─▶ J05 ─┐
                    ├─▶ J06 ─┼─▶ J07 ─▶ J08 ─▶ J09
M3                  ├─▶ J10 ─────────────────────┴─▶ J11
M4                  ├─▶ J12 ─┐
                    └─▶ J13 ─┴─▶ J14 (needs J09)
M5                                J14 ─▶ J15 ─▶ J16
M6          J09 + J11 ─▶ J17 ─▶ J18 ─▶ J19 ─▶ J20 ─▶ J21
M7                                                  J21 ─▶ J22
M8                                                         J22 ─▶ J23
M9                                                         J22 ─▶ J24, J25 (parallel with J23)
M10                                                        J23 + J24 + J25 ─▶ J26
M11                                                        J26 ─▶ J27
M12                                                        J27 ─▶ J28 ─▶ J29 (J29 builds in parallel, merges after J28)
```

After J03, the forecast track (J05–J09), the Claude track (J10), and the
ground-truth track (J12–J13) run in parallel. Production (J17–J21) needs the
forecast and Claude tracks, not ground truth: the page ships with the
accumulation empty state if J14 is not done.

## Jobs

### M1 — Skeleton

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J01 | `spec-agent-sdk` | Switch the Claude spec to the Claude Agent SDK with a `claude setup-token` token: agent-runs call conventions, technical-design §9 / §14 / §17 #4, hosting-frank secret name, decisions row. Spike: one structured-output call, no tools, writable `HOME` under a read-only rootfs | Spike returns schema-valid JSON inside a `read_only` container; specs updated |
| J02 | `app-skeleton` | `pyproject.toml` (uv, Python 3.13), `src/hazelst/` layout, `config.py`, `cli.py`, ruff / mypy / pytest config, `ci.yml` (lint, types, tests) | `uv run hazelst --help` works; CI green |
| J03 | `db-schema` | `db.py` (WAL, `user_version`), `migrations/0001_init.sql` with every table in data-model.md, `run` rows, job lock | Migrate from empty and re-run is a no-op; lock test passes |
| J04 | `image-release` | Starlette app with `/health` and `/metrics`; multi-stage Dockerfile (65532, read-only, tmpfs `/tmp`); smoke job; `image.yml` pushing `ghcr.io/jstockdi/hazelst:<sha>` | Image digest in GHCR (public); smoke passes on the built image |

### M2 — Forecast and numbers-only page

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J05 | `source-open-meteo` | Summit and base fetch for every mountain, model run times from `meta.json`, raw `fetch` rows (gzip, SHA dedupe), normalize to `forecast_hourly`; `hazelst record` for fixtures | Normalize tests on recorded fixtures; a live ingest fills `forecast_hourly` |
| J06 | `source-nws` | Points cache, gridpoint snow / ice, alerts, AFDs (BTV, ALY, GYX) with whitespace normalization | Normalize tests on fixtures; storm-word extraction test on a wrapped AFD |
| J07 | `forecast-derive` | Windows from 6 AM, model mean, range, confidence, days, tags, `precip_type`, stale-model exclusion, NWS cross-check; port from `prototype/fetch.py` | Unit tests (daily buckets sum to windows, each tag, each precip type); output matches the 2026-11-25 sample run |
| J08 | `page-render` | Jinja templates, SVG charts, "What falls" grid, window table, accumulation empty state, self-hosted fonts; routes `/`, `/d/{date}`, `/archive`, `/api/report/{date}.json` | Golden HTML test; page at phone width matches mockup C |
| J09 | `job-schedule` | APScheduler jobs and editions (7:00, 8:15, 2:40), ECMWF 06Z gate (retry to 8:45), idempotency, startup catch-up, `numbers_only` `report-build` | `hazelst run <job> --now` for each scheduled job, in schedule order, in the local container against live data, publishes three `numbers_only` editions; scheduler unit test fires each cron at the right local time across a DST change |

### M3 — Claude report

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J10 | `agent-client` | Agent SDK wrapper per J01, Pydantic schemas, `agent_call` logging, `HAZELST_ENABLE_AGENTS`, stub client for CI | Schema round-trip tests with the stub; one live call logged locally |
| J11 | `report-write` | Digest, role prompts, `.claude/skills/hazel-st/SKILL.md`, `report-write` → `report-check` → `report-revise`, `numbers_only` fallback with 15-minute rebuild until 9 AM | The three sample-run precip-type errors fail `report-check`; `hazelst run report-build --now` with live data publishes a checked narrative |

### M4 — Ground truth

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J12 | `source-obs` | PNS, IEM LSR, NOHRSC → `observation` / `snow_report`, using the endpoints and fixtures from J00 ([data-sources.md](data-sources.md#observation-and-backfill-endpoints)) | Normalize tests on fixtures; a live ingest stores observations near each mountain |
| J13 | `resort-adapters` | The three shared parsers and four presets mapped in J00 ([data-sources.md](data-sources.md#resort-adapters)); fixtures in `tests/fixtures/resorts/`; 3 fetches per resort per day; identifying `User-Agent`; contract tests; `conditions-extract` only for non-Boyne failures | Contract tests on saved pages; a live ingest fills `observation` (`source = resort`) for all eight |
| J14 | `forecast-verify` | Per-edition scoring at 9 PM (HIT / MISS / NO_OBS), observation priority; accumulation section on the page | A day with observations produces `verification` rows for each edition; accumulation chart renders |

### M5 — Learning

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J15 | `skill-learn` | Nightly `skill` rebuild; `corrected` method behind the n ≥ 10 and "beats raw mean on 20 days" gate | Gate tests on synthetic verification data |
| J16 | `knowledge-review` | Weekly `knowledge-propose`; CLI `knowledge accept` / `config accept`; `hazelst replay <date>` | Proposals stored as `proposed`; accepted knowledge reaches the `report-write` prompt |

### M6 — Production on frank

| Job | Name | Scope | Owner gate | Done when |
|-----|------|-------|------------|-----------|
| J17 | `frank-stack` | frank PR from a worktree off `origin/main` (frank `main` requires signed commits and allows squash merges only): `projects/hazelst/compose.yml` (digest-pinned), `secrets.enc.env` (SOPS), `systemd/frank-hazelst.service` | P1 token; P4(b) merge | PR merged |
| J18 | `frank-deploy` | Confirm `ordinance-member-stack` is in `main`, then `git switch main && git pull` in `/opt/frank` (P3); create and mount `lv-hazelst` (10 GB) at `/srv/hazelst` (65532, 0750), decrypt the secret, `make deploy STACK=hazelst`, enable the unit | P4(c) | Container healthy on frank; `/health` 200 on the `edge` network |
| J19 | `frank-ingress` | Caddyfile site and tunnel ingress rule (frank PR, merged, pulled into `/opt/frank`), DNS route for `hazelst.thmxtp.com`, platform restart | P4(d) (restarts the shared platform stack) | Public hostname serves the page |
| J20 | `ops-hardening` | Nightly `sqlite3 .backup`, report-age alert, rollback to the previous digest rehearsed | — | Backup file exists; rollback and roll-forward done once |
| J21 | `burn-in` | Scheduled check at 7:20 AM ET on three consecutive mornings: today's report status, edition times, last night's `forecast-verify` run, backup file; posts a one-line result each morning | — | Report by 7:15 AM ET on 3 consecutive days; `forecast-verify` ran each night |

### M7 — Backfill

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J22 | `season-backfill` | Open-Meteo Previous Runs (GFS, GEM; ECMWF has no snowfall at lead) and IEM LSR / PNS for the prior season into the same tables; seed `skill` | `skill` has rows per mountain × backfilled model; backfill is idempotent |

### M8 — Resort-reported history

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J23 | `snocountry-history` | New source `sources/snocountry.py`: `GET https://good-cormorant-17.convex.site/api/resort-data?resortId={id}` (fallback `https://snocountry.com/.netlify/functions/snowreport-resort?resortId={id}`), one call per mountain, raw response kept in `fetch`. `snocountry_id` per mountain in `config/mountains.json` (Killington 802007, Pico 802014, Stratton 802019, Sugarbush 802023, Cranmore 603007, Loon 603014, Sugarloaf 207008, Sunday River 207009). Rows go to `observation` with `source = 'snocountry'` per data-sources.md [SnoCountry](data-sources.md#snocountry). Migration 0003: unique index on `observation (mountain, source, obs_date)` for `source = 'snocountry'`; writes upsert. `season-backfill` loads the full history (2019-11 onward for new snow, 2012 onward for base depth); `ingest-obs` fetches it at each run as the resort fallback. `forecast-verify` source order: resort > snocountry > PNS / LSR > NOHRSC. After merge: deploy to frank, run the backfill for 2019-11-01..2026-04-30, re-run `forecast-verify` and `skill-learn` over the backfilled range | `observation` has `snocountry` rows for all 8 mountains; counts per mountain per season match the source (tests on saved fixtures); inferred zeros only on base-depth days; re-running the backfill leaves counts unchanged; `verification.observed_source = 'snocountry'` appears for backfilled days; `skill` rebuilt |

### M9 — Elevation-aware ground truth

J24 and J25 run in parallel with J23. Migration numbers are fixed: J23 0003,
J25 0004.

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J24 | `station-truth` | New source `sources/ghcn.py`: GHCN-Daily `SNOW` and `SNWD` from the NCEI Access Data Service (`https://www.ncei.noaa.gov/access/services/data/v1?dataset=daily-summaries&stations={ids}&dataTypes=SNOW,SNWD&startDate=…&endDate=…&format=json&units=standard`, no token). Stations: every GHCN station within 20 mi of a mountain's configured point with `SNOW` reported since 2024-01-20 (from `ghcnd-stations.txt` and `ghcnd-inventory.txt`), plus Mount Washington `USW00014755` for Loon, Cranmore, Sunday River, and Sugarloaf; the list is generated once and committed as `config/stations.json` (id, name, lat, lon, elev_ft, mountain, distance_mi). Rows go to `snow_report` per data-sources.md [GHCN-Daily stations](data-sources.md#ghcn-daily-stations). `season-backfill --sources ghcn` loads 2024-01-20 onward; `ingest-obs` fetches the last 10 days each run (co-op reports arrive late). Verification eligibility is unchanged (the existing 5 mi PNS / LSR rule, reported as `observed_source = 'ghcn'`); stations beyond 5 mi are context, not truth. `hazelst stations summary --season 2025` prints, per mountain, season snowfall per station sorted by elevation, next to the SnoCountry season total | `config/stations.json` committed; `snow_report` has GHCN rows with `elev_ft` for every listed station with data; rerun leaves counts unchanged; the summary prints for all 8 mountains; tests on saved fixtures |
| J25 | `report-elevation` | Every point observation carries an elevation; data-model.md gains the column with the migration (the schema test compares them). `snow_report.elev_ft` for PNS / LSR rows comes from the Open-Meteo elevation API (`https://api.open-meteo.com/v1/elevation?latitude=…&longitude=…`, up to 100 points per call) at insert, and a one-off `hazelst elevations fill` fills existing rows. Migration 0004 adds `verification.observed_elev_ft INT`: the report's elevation for `pns` / `lsr`, the pixel-center elevation for `nohrsc` (looked up once per mountain and stored in `config/mountains.json` as `nohrsc_pixel_elev_ft`), NULL for `resort` / `snocountry` (measurement site unknown). `hazelst verify summary --by elevation` prints n, bias ratio, MAE, and hit rate per mountain per band (< 1,500 ft, 1,500–2,500 ft, > 2,500 ft, unknown). After merge: deploy, fill, re-run `forecast-verify` for 2024-01-20..2026-04-30 | No PNS / LSR row has NULL `elev_ft` (except failed lookups, counted); `observed_elev_ft` set on every `pns` / `lsr` / `nohrsc` verification row; the summary prints on frank |

### M10 — Resort-reported truth

Owner decision 2026-11-26 (decisions row 24): each mountain is scored only
against its own resort's reported snow; every other source is still collected
and informs, but never stands in for it.

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J26 | `resort-truth` | `verify.observed()` returns only resort-reported snow: the live resort report (`resort`, `resort_llm` as today), else the `snocountry` row for the same obs_date (the same resort number, republished). No PNS / LSR / GHCN / NOHRSC value becomes `observed_in`; a day without a resort-reported value is `NO_OBS`. Migration 0005 adds `verification.context JSON`: every other source's value for the mountain-day (NOHRSC 24 h at the pixel; each PNS / LSR / GHCN report within 20 mi with place, `snow_in`, `elev_ft`, `distance_mi`), same day window as today; data-model.md gains the column in the same PR. `skill-learn` follows (it reads the scored truth). Accumulation: resort-reported values only; a day without one shows "no report"; the other sources appear beside it, labelled with source and elevation, never summed into it (update product/report-page.md). The `report-write` digest carries the context labelled as not resort-reported. Remove the NOHRSC accumulation fallback. After merge: deploy, re-run `forecast-verify` for 2024-01-20..2026-04-30 (and the `backfill` edition), `skill-learn` | Tests: observed() never returns a non-resort source; `context` filled per source; accumulation shows "no report" with context beside it. On frank: `verification.observed_source` over 2024-01-20..2026-04-30 is only `resort` / `resort_llm` / `snocountry`; `skill` rebuilt; today's page renders |

### M11 — Resort snow ratio

Owner decision 2026-11-26 (decisions row 26). On the backfill edition
(2024-01-20..2026-04-30, 946 scored snow days) the model mean is 0.53 of the
resort-reported snow. Open-Meteo converts snow water to snowfall at a fixed
7:1 (0.7 cm per mm), while resort snow per inch of forecast snow water is
11.2 overall and 9.2 (Sugarloaf) to 14.8 (Pico) by mountain, so the gap is
mostly the snow ratio, not the water amount.

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J27 | `resort-ratio` | New forecast method `resort_ratio`: each model's hourly summit snow water (`snowfall_in / 7`, which keeps the model's rain / snow split) times a ratio fitted for that resort, summed into days and windows like `raw_mean` (mean of models, min–max range, same stale rule). Ratio per mountain by summit 2 m temperature band (≤ 15 °F, 15–25 °F, 25–32 °F, > 32 °F), split by `nw_upslope` (forecast-method.md, Pattern tags) where a mountain has ≥ 15 snow days on each side; a band or split with fewer than 15 snow days uses the all-mountain value for that band. `storm_type` is not used (it comes from NWS discussions, which the backfill does not have). Fit: least squares of resort-reported 6 AM → 6 AM snow against forecast snow water on lead-day-1 snow days, per model and for the mean. Migration 0006 adds a `snow_ratio` table (mountain, model, temp band, upslope, ratio, n, fitted_through); data-model.md gains it in the same PR. `skill-learn` refits it nightly with the same D − 2 rule. `season-backfill` adds `temperature_850hPa`, `relative_humidity_850hPa`, `wind_speed_850hPa`, `wind_direction_850hPa` to the Previous Runs fetch so `nw_upslope` can be computed for backfilled days; re-run the backfill for 2024-01-20..2026-04-30. `report-build` stores `resort_ratio` windows and days; `forecast-verify` scores them beside `raw_mean` and each model (live editions and the `backfill` edition). Page: at a mountain where `resort_ratio` beats `raw_mean` (lower 1d MAE over the mountain's last 20 verified days, every edition where both were scored), the headline, range, chart, and table use `resort_ratio`, labelled "resort ratio"; elsewhere `raw_mean` stays. Update forecast-method.md (Snow amount) and product/report-page.md. New `hazelst ratio backtest --fit-to 2025-04-30 --test-from 2025-11-01`: fits on days through `--fit-to` only, scores 2025-26 lead-day-1 snow days against resort reports, and prints per mountain n, bias ratio, MAE, and hit rate for `raw_mean`, flat 10:1, `resort_ratio` on temperature only, `resort_ratio` with the upslope split, and `corrected`. After merge: deploy, backfill, `skill-learn`, `forecast-verify` for 2024-01-20..2026-04-30, then run the backtest on frank | Tests on fixtures: ratio fit, band fallback to the all-mountain value, `resort_ratio` days sum to its windows. On frank: `snow_ratio` filled for all 8 mountains; the backtest table printed with the 2025-26 numbers for every method; `verification` has `resort_ratio` rows; today's page renders and names the method used per mountain |

### M12 — Method choice and finer models

Owner decision 2026-11-27 (decisions row 28). The J27 backtest on the
held-out 2025-26 season: `resort_ratio` MAE 1.75″ vs `raw_mean` 1.86″, bias
0.75 vs 0.46. The live gate (last 20 verified days) switched four mountains
on late-spring backfill days with tiny totals (Cranmore by 0.007″), and the
remaining 25% low bias is likely precipitation the coarse global models
(13–25 km) miss over the mountains.

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J28 | `method-gate` | Replace the last-20-days gate for `resort_ratio` and `corrected` with a held-out gate. New table `method_gate` (mountain, method, test_from, test_to, n, mae_in, raw_mean_mae_in, chosen, decided_at) via migration 0007; data-model.md gains it in the same PR. `hazelst method choose` fits on every complete season before the newest complete season, tests on that season (lead-day-1 resort-reported snow days, same day set for every method, as `ratio backtest`), and marks a method chosen at a mountain when its MAE is at least 0.05″ below `raw_mean`'s and lowest among the candidates; ties keep `raw_mean`. Once the current season has ≥ 20 live verified snow days at a mountain (editions other than `backfill`), the choice uses those live days only, same margin. `skill-learn` re-runs the choice nightly. The page reads `method_gate` and names the method per mountain. Also: fix `hazelst schedule` (`KeyError: 'report-rebuild'`); record in data-sources.md that Previous Runs GFS lead-day-1 snowfall is empty before 2024-12-06. Update forecast-method.md ("Where it shows", Corrected value). After merge: deploy, `method choose`, rebuild today's page outside the publish windows | On frank: `method_gate` filled for all 8 mountains with the 2025-26 numbers; the chosen set matches the J27 backtest with the 0.05″ margin (expected `resort_ratio` at Sugarbush, Sunday River, Loon, Cranmore, Sugarloaf; `raw_mean` at Killington, Pico, Stratton); `hazelst schedule` prints; the page shows the chosen method per mountain |
| J29 | `fine-models` | Add the finer models from Open-Meteo: ECMWF IFS HRES 9 km, NCEP HRRR CONUS (3 km), GEM HRDPS Continental (2.5 km), NCEP NBM CONUS (native snowfall), and ECMWF AIFS; confirm each model id from Open-Meteo's docs and `meta.json`. Live: `ingest-forecast` fetches them every run at summit and base (record forecast length and update cycle in data-sources.md); always collected. Backfill: the Historical Forecast API (`https://historical-forecast-api.open-meteo.com/v1/forecast`), which stitches short-lead output, so it stands in for the day-1 forecast only; load 2023-11-01..2026-04-30 at summit, paced under Open-Meteo's limits. Fit `snow_ratio` cells per model as J27 (NBM's native snowfall is also scored as is). Candidates for each mountain's day 1: `raw_mean`, `resort_ratio` (global models), each fine model with its ratio, the mean of the fine models with their ratios (`fine_ratio`), and NBM raw. Days 2–5 stay on the global models (HRRR and HRDPS end at 18–48 h). Extend `ratio backtest` (fit through 2025-04-30, test 2025-26) to print every candidate per mountain; J28's `method choose` takes the new candidates. Merge after J28 (rebase on it). After merge: deploy, backfill, `skill-learn`, `forecast-verify` for 2023-11-01..2026-04-30, backtest, `method choose` | On frank: `forecast_hourly` has the fine models live and backfilled (rows per model per season printed); the backtest table printed for every candidate per mountain; `method_gate` updated; the page renders and names each mountain's day-1 method |
| J30 | `storage` | Store fewer past hours: `ingest-forecast` keeps `forecast_hourly` rows from 6 AM local on the ingest date (raw responses unchanged; forward-only, no rows deleted); show every reader of past hours is unchanged (report-build for the 0700 and 1440 editions, the finer models' day 1, `skill-learn`, `snow_ratio`). Backups weekly (Sunday 4:00 AM ET), keep the newest 4; frank's backup-age alert to 8 days. `hazelst method choose` prints a `role` column (`headline`, `beneath`, `day1`) instead of `chosen`. Decisions row 31. After merge: deploy once the J29 backfill is done, prune frank's backups to the newest 4 | On frank: rows per ingest about halved (measured before and after); `hazelst schedule` shows the Sunday backup; at most 4 backups; `method choose --dry-run` prints roles |
| J31 | `snow-timeline` | Replace the stacked forecast chart, the precipitation-type grid, and the "By day" accumulation details with one phone-first snow timeline: 3 resort-reported days │ now rule │ 4 forecast days, one row per mountain, palette D, whole inches from 1″ and tenths below, a `title` on every cell. Move the date and edition from the band to the footer. Keep the forecast table; remove the accumulation section (the past columns cover it); JSON payload unchanged. report-page.md, decisions row 32. After merge: deploy, rebuild today's page with `report-refresh` | Render tests for no-report and zero cells, rounding, palette bins, row order, and 390 px-safe markup; today's page renders at 390 px and 728 px with no horizontal scroll (screenshots on the PR); live page shows the timeline |
| J33 | `report-check` | Make the written report publish because the code is correct. Checker: bind each precip word and snow number to its own phrase (split on "with", "while", "but", ";", list commas, "and"), read each word at the elevation its phrase names. Data: per-elevation `base_precip_type` and `freezing_rain_at` from `tags.day_precip`; the digest drops the precip tags. Remove numbers-only (status, headline, banner, cautions, payload state); a failed write or a draft still failing after 3 revisions fails the job and alerts (`hazelst_report_job_failed`, frank `HazelstReportFailed`); refreshes revise the morning draft up to 3 rounds. Headline as a ≤ 12-word text alert, checked. Regression corpus of every production draft (`tests/fixtures/report/corpus/`). Decisions row 33 | All 6 stored `report-write` / `report-revise` outputs pass, including runs 147 and 181; the morning drafts re-checked on later numbers fail only on pinned genuine errors; after deploy, `report-refresh` for 2026-11-27 on frank publishes a page with prose |
| J35 | `page-overhaul` | Rebuild the report page to the owner-approved option "ab": light theme (grey page, white bordered cards, Fraunces headline, Inter 15 / 13 / 12); the snow grid with tinted forecast columns, filled cells from 0.5″, the global models' range in the cell when they disagree; a tap-to-open model drawer per mountain aligned to the day columns with CSS subgrid (Hazel St, GFS, ECMWF, GEM, finer models with data, NWS 1d and 3d), keyboard accessible; remove the forecast table and method note; written report after the grid; one-line footer. Payload adds `forecast.days[].by_model` (backward compatible). report-page.md, decisions row 34. After merge: deploy, rebuild today's page with `report-refresh` | Render tests for the spread rule, drawer rows, aligned columns, and no table; today's stored payload rendered at 390 and 728 px matches the mockup side by side with no horizontal scroll at 360, 390, or 728 px; live page shows the drawer |

## Owner actions

All owner input happens in pre-flight, so threads do not stop:

| When | Action |
|------|--------|
| Pre-flight P1 | Run `claude setup-token`; hand over the token through the secret prompt (never in chat) |
| After J16, weekly | Accept or reject `knowledge` and `config_change` proposals (not a build blocker) |
