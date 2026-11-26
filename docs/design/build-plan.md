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
| P3 | frank checkout: `/opt/frank` is on branch `ordinance-member-stack`, 14 commits ahead of `origin/main`, no PR open. `ordinance-member-stack` merges to `main` first; then J17 opens the hazelst frank PR against `main`, and J18 switches `/opt/frank` to `main` (which then contains both) before deploying | Owner decided 2026-11-26 (merge first); merge pending |
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
| A resort page is blocked or has no parseable data | Disable that adapter; observations fall back to PNS / LSR / NOHRSC; record in `data-sources.md` |
| ECMWF 06Z not available by 8:45 | The 8:15 edition publishes with the 00Z run and says so in the footer |
| Open-Meteo Historical Forecast lacks a model or season | Backfill the models and dates it has |
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
| J12 | `source-obs` | PNS, IEM LSR, NOHRSC → `observation`; confirm the IEM and NOHRSC endpoints first | Normalize tests on fixtures; a live ingest stores observations near each mountain |
| J13 | `resort-adapters` | Map the eight report pages; declarative adapters (JSON first, selectors only where needed); 3 fetches per resort per day; identifying `User-Agent`; contract tests; `conditions-extract` only for non-Boyne failures | Contract tests on saved pages; a live ingest fills `snow_report` for all eight |
| J14 | `forecast-verify` | Per-edition scoring at 9 PM (HIT / MISS / NO_OBS), observation priority; accumulation section on the page | A day with observations produces `verification` rows for each edition; accumulation chart renders |

### M5 — Learning

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J15 | `skill-learn` | Nightly `skill` rebuild; `corrected` method behind the n ≥ 10 and "beats raw mean on 20 days" gate | Gate tests on synthetic verification data |
| J16 | `knowledge-review` | Weekly `knowledge-propose`; CLI `knowledge accept` / `config accept`; `hazelst replay <date>` | Proposals stored as `proposed`; accepted knowledge reaches the `report-write` prompt |

### M6 — Production on frank

| Job | Name | Scope | Owner gate | Done when |
|-----|------|-------|------------|-----------|
| J17 | `frank-stack` | frank PR from a worktree off `origin/main`: `projects/hazelst/compose.yml` (digest-pinned), `secrets.enc.env` (SOPS), `systemd/frank-hazelst.service` | P1 token; P4(b) merge | PR merged |
| J18 | `frank-deploy` | Confirm `ordinance-member-stack` is in `main`, then `git switch main && git pull` in `/opt/frank` (P3); create and mount `lv-hazelst` (10 GB) at `/srv/hazelst` (65532, 0750), decrypt the secret, `make deploy STACK=hazelst`, enable the unit | P4(c) | Container healthy on frank; `/health` 200 on the `edge` network |
| J19 | `frank-ingress` | Caddyfile site and tunnel ingress rule (frank PR, merged, pulled into `/opt/frank`), DNS route for `hazelst.thmxtp.com`, platform restart | P4(d) (restarts the shared platform stack) | Public hostname serves the page |
| J20 | `ops-hardening` | Nightly `sqlite3 .backup`, report-age alert, rollback to the previous digest rehearsed | — | Backup file exists; rollback and roll-forward done once |
| J21 | `burn-in` | Scheduled check at 7:20 AM ET on three consecutive mornings: today's report status, edition times, last night's `forecast-verify` run, backup file; posts a one-line result each morning | — | Report by 7:15 AM ET on 3 consecutive days; `forecast-verify` ran each night |

### M7 — Backfill

| Job | Name | Scope | Done when |
|-----|------|-------|-----------|
| J22 | `season-backfill` | Open-Meteo Historical Forecast and IEM LSR / PNS for the prior season into the same tables; seed `skill` | `skill` has rows per mountain × model; backfill is idempotent |

## Owner actions

All owner input happens in pre-flight, so threads do not stop:

| When | Action |
|------|--------|
| Pre-flight P1 | Run `claude setup-token`; hand over the token through the secret prompt (never in chat) |
| Pre-flight P3 | Merge `ordinance-member-stack` into frank `main` (J17 waits for it) |
| After J16, weekly | Accept or reject `knowledge` and `config_change` proposals (not a build blocker) |
