# Technical Design

The build-ready design for Hazel St Forecast: one Python service on frank that
ingests free weather data into SQLite, writes a daily ski report with Claude,
checks it with code, serves it as a mobile web page, and learns from every
day's verified results.

Detail lives in the linked specs; this document is the map and the decisions.

| Topic | Spec |
|-------|------|
| What the page shows | [report-page.md](../product/report-page.md), [mountains.md](../product/mountains.md) |
| Where data comes from | [data-sources.md](data-sources.md) |
| How numbers are computed | [forecast-method.md](forecast-method.md) |
| How the prose is produced and kept honest | [narrative.md](narrative.md) |
| Jobs, schedule, and Claude roles | [agent-runs.md](agent-runs.md) |
| Database | [data-model.md](data-model.md) |
| Release, deploy, operations | [hosting-frank.md](../operations/hosting-frank.md) |
| Decisions log | [decisions.md](decisions.md) |

---

## 1. Goals and non-goals

**Goals**
- A Hazel St Forecast page every morning by 7:15 AM ET, Nov 15 – Apr 30, for
  the 8 mountains in [mountains.md](../product/mountains.md).
- 1d / 3d / 5d forecasts with honest ranges, precip type, wind holds, and
  accumulation — every claim traceable to stored data.
- Keep all data; get measurably better through the season (verification →
  skill → corrected forecast; proposed knowledge with owner approval).
- Run unattended on frank under its existing conventions.

**Non-goals (v1)**
- Accounts, notifications, email, or push.
- Mountains outside the roster; summer operation.
- An admin web UI (the CLI over `docker exec` is the admin surface).
- Real-time lift status beyond what resort reports carry.

## 2. System context

```
                    ┌──────────── frank (Docker, edge network) ─────────────┐
 Phone ── HTTPS ──▶ Cloudflare ─▶ cloudflared ─▶ Caddy ─▶ hazelst:8080      │
                    │                                    │                  │
                    │                                    ├─ web (read-only) │
                    │                                    ├─ scheduler/jobs  │
                    │                                    └─ /data/hazelst.db│
                    └────────────────────────────────────┬──────────────────┘
                                                         │ HTTPS egress
        Open-Meteo · api.weather.gov · IEM · NOHRSC · resort sites · api.anthropic.com
```

One container, one process, one SQLite file. No inbound ports beyond Caddy on
the `edge` network.

## 3. Stack

| Concern | Choice | Why |
|---------|--------|-----|
| Language | Python 3.13 | Same as the sailing report; strong data and HTTP tooling |
| Packaging | `uv` + `pyproject.toml`, locked; `[tool.uv] exclude-newer` holds resolution to releases at least two weeks old | Fast, reproducible image builds |
| HTTP client | `httpx` (sync) with a small retry helper | Timeouts, HTTP/1.1, simple |
| Schemas | Pydantic v2 | One model per JSON shape; also generates Claude output schemas |
| Database | stdlib `sqlite3`, WAL, numbered SQL migrations, `PRAGMA user_version` | No ORM; SQL is the spec |
| Web | Starlette + Jinja2 + uvicorn (1 worker) | Small; server-rendered pages; JSON routes |
| Scheduler | APScheduler 3 `BackgroundScheduler`, `CronTrigger(timezone=America/New_York)` | In-process, DST-aware |
| HTML parsing (resort adapters) | `selectolax` | Fast, small |
| Charts | Hand-built inline SVG (ported from `prototype/render.py`) | No plotting stack in the image |
| Claude | Claude Agent SDK (`claude-agent-sdk`, bundles the Claude Code CLI) with a `claude setup-token` token; `claude-opus-5-5`; structured outputs | See [agent-runs.md](agent-runs.md) |
| Metrics | `prometheus_client` at `/metrics` | frank Phase 5 monitoring can scrape it |
| Fonts | Fraunces + Inter (OFL), self-hosted in the image | No third-party requests from the page |
| Lint / types / tests | `ruff`, `mypy`, `pytest` | |

## 4. Repository layout

```
hazelst/
  pyproject.toml  uv.lock  Dockerfile  .dockerignore
  src/hazelst/
    config.py            # env + mountains.json + accepted config_change overrides
    db.py                # connect (WAL, FKs), migrate, job lock
    migrations/0001_init.sql ...
    models.py            # Pydantic: ReportPayload, ForecastWindow, ForecastDay, ...
    sources/             # one module per source: fetch() -> raw, normalize() -> rows
      open_meteo.py  nws.py  discussions.py  iem.py  nohrsc.py
      resorts/  base.py  killington.py  sugarbush.py  ...
    derive.py            # forecast_hourly -> forecast_window, forecast_day (+ tags, precip_type)
    tags.py              # pattern tag rules
    verify.py            # forecast-verify
    learn.py             # skill-learn (SQL + bias/weights)
    corrected.py         # corrected forecast method
    digest.py            # DB -> report-write input
    agents/
      client.py          # one place for SDK options, model, effort, logging, daily call cap
      prompts/           # role prompts (versioned by SHA)
      report_write.py  report_revise.py  conditions_extract.py  knowledge_propose.py
    report_check.py      # deterministic claim checks
    render/  page.py  charts.py  templates/  static/fonts/
    web.py               # Starlette app
    jobs.py              # job functions (ingest-forecast, report-build, ...)
    scheduler.py         # cron wiring + startup catch-up
    cli.py               # hazelst serve|run|rebuild|replay|knowledge|backup|record
  config/mountains.json
  .claude/skills/hazel-st/SKILL.md     # regional knowledge; part of the report-write system prompt
  .claude/commands/                    # /forecast, /powder, /mountain (call the CLI locally)
  tests/  fixtures/  unit/  contract/  golden/
  prototype/                           # the 2026-11-25 proof; removed once ported
  docs/
  .github/workflows/ci.yml  image.yml
```

## 5. Module boundaries

The rule: **sources never compute, derive never fetches, agents never touch the
network except Claude, web never writes.**

| Module | Input | Output | Notes |
|--------|-------|--------|-------|
| `sources/*.fetch` | config, run | `fetch` rows (raw, gzip) | One HTTP concern each; per-source timeout and retries |
| `sources/*.normalize` | a `fetch` row | normalized rows | Pure; rerunnable over history (`hazelst rebuild`) |
| `derive` | `forecast_hourly`, `nws_grid` for a run | `forecast_window`, `forecast_day` | Pure functions over DB rows; property-tested |
| `digest` | DB for a date | `ReportDigest` (Pydantic) | The only input `report-write` sees |
| `agents/*` | typed input | typed output + `agent_call` row | No DB writes beyond `agent_call` |
| `report_check` | `ReportDraft`, `ReportDigest` | list of errors | Pure; the publish gate |
| `render` | `ReportPayload` + narrative | HTML | Pure; golden-tested |
| `web` | DB (read-only connection) | HTTP | Cached until the next job commits |
| `jobs` | — | orchestrates the above in a `run` | Holds the job lock |

## 6. Data flow — one morning

```
06:45 ingest-forecast
  open_meteo.fetch (summit, base) ─┐
  nws.fetch (grids, alerts) ───────┼─▶ fetch (raw) ─▶ normalize ─▶ forecast_hourly, nws_grid, alert
  discussions.fetch (BTV/ALY/GYX) ─┘                              └▶ discussion
07:00 report-build
  derive ─▶ forecast_window (raw_mean[, corrected]) + forecast_day (tags, precip_type)
  digest ─▶ report-write ─▶ report-check ─✗─▶ report-revise ─▶ report-check
                                       └✓──────────────────────────┴─▶ report (published)
      (06:50 ingest-obs ─▶ observation, snow_report: overnight snow, base, open terrain)
08:10 ingest-forecast + ingest-obs ─▶ 08:15 report-refresh (ECMWF 06Z, late resort reports)
14:30 ingest-forecast + ingest-obs ─▶ 14:40 report-refresh (12Z models, afternoon discussions)
21:00 forecast-verify ─▶ verification per edition (HIT within ±2″ or ±30%, else MISS; NO_OBS if no source)
02:00 skill-learn ─▶ skill
Mon 03:00 knowledge-review ─▶ knowledge / config_change (proposed)
```

## 7. Configuration

| Source | Holds | Changes by |
|--------|-------|-----------|
| Environment | `CLAUDE_CODE_OAUTH_TOKEN` (secret), `HOME` (`/tmp/home`, writable for the Claude CLI), `NWS_USER_AGENT`, `TZ`, `HAZELST_DB`, `HAZELST_AGENT_DAILY_CALLS` (default 10), `HAZELST_ENABLE_AGENTS` | frank compose / SOPS |
| `config/mountains.json` (in image) | Roster, coordinates, elevations, thresholds, adapters, `season_open` | PR + release |
| `config_change` (accepted rows) | Threshold and weight overrides learned in season | `hazelst config accept <id>` |
| `agents/prompts/*` + `SKILL.md` (in image) | Role prompts, regional knowledge | PR + release |

Effective config = file values overlaid by accepted `config_change` rows,
logged at the start of each run.

## 8. HTTP surface

| Route | Returns | Cache |
|-------|---------|-------|
| `GET /` | Today's report (latest if not built yet) | Until next commit |
| `GET /d/{date}` | Report for a date (latest version) | Immutable after the day ends |
| `GET /archive` | Dates, headlines, verification results | Until next commit |
| `GET /api/report/{date}.json` | `ReportPayload` | Same as page |
| `GET /health` | `200` if DB opens and the last `report-build` did not fail | none |
| `GET /metrics` | Prometheus metrics | none |

Public and read-only. No cookies, no analytics, no third-party requests.

## 9. Claude integration

Summarized from [agent-runs.md](agent-runs.md):

- Roles: `report-write`, `report-revise`, `conditions-extract`,
  `knowledge-propose`. `report-check` is code.
- `claude-opus-5-5` for every role, effort set per role
  (`high` / `medium` / `low` / `high`). Model and effort per role are config.
- Claude Agent SDK (`claude-agent-sdk`): each call runs the bundled Claude Code
  CLI as a subprocess, authenticated with `CLAUDE_CODE_OAUTH_TOKEN` from
  `claude setup-token`; usage counts against the owner's subscription.
- No tools (`tools=[]`), no filesystem settings (`setting_sources=[]`), no
  skills or MCP servers, no session persistence; `max_turns=5`.
- Structured output via `output_format` (`json_schema` from the role's Pydantic
  model); validate `ResultMessage.structured_output` on return; a non-`success`
  `subtype`, `is_error`, or a validation error is a failed call.
- Stable system prompt (role prompt + `SKILL.md` + accepted knowledge) passed as
  one string and cached by the CLI; digest after it.
- Every call logged in `agent_call` with prompt SHA, input, output, tokens,
  and the SDK's cost estimate.
- Daily call cap (`HAZELST_AGENT_DAILY_CALLS`, default 10); at the cap the page
  is `numbers_only`.
- `HAZELST_ENABLE_AGENTS=false` produces `numbers_only` pages (used in CI and
  local runs without a token).

## 10. Testing

| Layer | What | How |
|-------|------|-----|
| Unit | Window/day math (daily buckets sum to windows), tags, precip type, NWS proration, confidence | `pytest`, small hand-built series |
| Regression | The three precip-type errors from the sample run must fail `report-check` | Fixture drafts |
| Normalize | Each source's parser on recorded responses | `tests/fixtures/<source>/*.json.gz` from `hazelst record` |
| Contract | Each resort adapter on saved pages; alert when a live page stops parsing | Fixture HTML + nightly live check in `ingest-obs` |
| Golden | Rendered HTML for a fixture day (text diff) | `tests/golden/` |
| Agents | Schema round-trip and `report-check` on stubbed outputs; no live calls in CI | Stub client |
| Smoke | Container starts read-only, migrates, `/health` 200, builds a `numbers_only` page from fixtures | CI job on the built image |

## 11. CI/CD

- `ci.yml` (every PR and push): `ruff`, `mypy`, `pytest`, Docker build, smoke test.
- `image.yml` (push to `main`): build `amd64`, push `ghcr.io/jstockdi/hazelst:<sha>`,
  print the digest. No `latest` tag.
- Deploy: a PR to the frank repo pins the digest in
  `projects/hazelst/compose.yml`; after merge, `make deploy STACK=hazelst` on
  frank. Rollback = previous digest. Details:
  [hosting-frank.md](../operations/hosting-frank.md).

## 12. Container

- `python:3.13-slim` multi-stage; final image has the venv, `src`, config,
  fonts; no compilers. The `claude-agent-sdk` wheel adds the Claude Code CLI
  binary (~320 MB) to the venv.
- User `65532:65532`, `read_only: true`, `tmpfs: /tmp`, `cap_drop: ALL`,
  `no-new-privileges`. Writes only to `/data` and `/tmp`; `HOME=/tmp/home` so
  the Claude CLI can write its config files on the tmpfs.
- `HEALTHCHECK` calls `/health` with the bundled Python (no curl in the image).
- Limits: 1 CPU, 512 MB.

## 13. Observability

- Logs: JSON lines to stdout — one per job step with run id, source, duration,
  rows, errors; one per Claude call with role, tokens, latency.
- Metrics: job duration and status, source failures by source, rows ingested,
  Claude calls, tokens, and estimated cost, report age, verification hit rate.
- Health: `/health` drives the Docker healthcheck; report age > 26 h is an alert
  condition for frank monitoring.

## 14. Security

- Secrets: only `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`; one-year
  lifetime, model requests only), SOPS-encrypted in the frank repo, decrypted
  to a 600 env file at deploy. Rotation: run `claude setup-token` again,
  re-encrypt, redeploy. The token is never logged.
- Claude calls run with no tools and no filesystem settings, so model output
  cannot run commands or read files in the container.
- No user input reaches Claude or SQL: routes take only a date (validated) — no
  query strings are used.
- Resort pages and discussion text are untrusted data: passed to Claude as
  data inside the digest, never as instructions; outputs are schema-validated
  and claim-checked.
- Egress allowlist documented in hosting-frank.md.

## 15. Performance and size

- Forecast ingest: ~3 s for all sources (measured 2026-11-25); obs ingest
  bounded by 8 resort pages.
- DB growth < 1 GB per season; no pruning.
- Page render < 50 ms from cache.

## 16. Milestones to production

| # | Milestone | Done when |
|---|-----------|-----------|
| M1 | Skeleton | Repo, `pyproject`, DB + migrations, CLI, `/health`, Dockerfile, CI green, image in GHCR |
| M2 | Forecast + numbers-only page | `ingest-forecast`, `derive`, render ported from the prototype; golden + unit tests; page matches the sample run |
| M3 | Claude report | `report-write`, `report-check`, `report-revise`, `agent_call`, daily call cap; numbers-only fallback tested |
| M4 | Ground truth | PNS + IEM LSR + NOHRSC; resort adapters (the five that return 200 first, then the three that redirect); `conditions-extract`; `forecast-verify`; accumulation section live |
| M5 | Learning | `skill-learn`, corrected method behind its gate, `knowledge-review`, CLI approvals |
| M6 | Production on frank | frank PR (compose, Caddy, tunnel, systemd, SOPS secret), deploy, smoke, nightly backup, rollback rehearsed |
| M7 | Backfill | Prior-season forecasts and reports loaded; `skill` seeded |

The jobs that deliver each milestone are in [build-plan.md](build-plan.md).

**Production means:** the public hostname serves today's report by 7:15 AM ET
on 3 consecutive mornings, `forecast-verify` has run each night, the nightly
backup exists, and a rollback to the previous digest has been rehearsed.

## 17. Production defaults (recommended, pending owner approval)

| # | Decision | Recommendation | Evidence |
|---|----------|----------------|----------|
| 1 | Repo and image | `jstockdi/hazelst`, **public** repo; image `ghcr.io/jstockdi/hazelst`, public | Personal project, like `jstockdi/bristol-harbor-sailing-report` (public). No secrets in the repo. A public image needs no registry login on frank (the existing `~/.docker-bcl` login is for `battle-creek-llc`) |
| 2 | Hostname | `hazelst.thmxtp.com` | frank's tunnel and Cloudflare zone already serve `play.thmxtp.com` and `bcl.thmxtp.com`; a subdomain is one ingress rule + one DNS route. A separate domain would need a new Cloudflare zone |
| 3 | Exposure | **Public** | Read-only page, no accounts or personal data; visitors never trigger Claude calls; Cloudflare in front |
| 4 | Claude auth and limits | Claude Agent SDK with a `claude setup-token` token on the owner's subscription (decisions row 15); in-app daily cap **10 calls** | A normal day uses 1–2 calls; the subscription has no per-token bill, so a call cap (not a dollar cap) bounds usage. The token is revocable on its own and lasts a year |
| 5 | Models | `claude-opus-5-5` for every role, effort per role through the SDK's `effort` option | `conditions-extract` is rare (adapter failures only) and disabled for Boyne sites, so a cheaper model would save little subscription usage |
| 6 | Storage | New LV **`lv-hazelst`, 10 GB**, at `/srv/hazelst` | frank gives each stack its own LV (`lv-terra-pg`, `lv-homestead-pg`, `lv-bcl-*`); `ubuntu-vg` has ~450 GB free. < 1 GB per season plus backfill fits with room |
| 7 | Host actions on frank | Approve the M6 runbook as one bundle: create and mount `lv-hazelst`, create `/srv/hazelst` (65532, 0750), decrypt the secret, `make deploy STACK=hazelst`, install `frank-hazelst.service`. I pause for a separate go-ahead before the ingress change (Caddyfile, tunnel config, DNS route), since it restarts the shared platform stack | frank is permission-gated; ingress is shared with mixtape and bcl |
| 8 | Resort sites | Code adapters only, 3 fetches per resort per day, identifying `User-Agent`. **No `conditions-extract` on Loon, Sunday River, or Sugarloaf** | robots.txt (2026-11-25): Killington, Pico, Sugarbush, Cranmore allow general crawling; Stratton has no rules. Boyne sites (Loon, Sunday River, Sugarloaf) publish Cloudflare content signals `search=yes, ai-train=no, use=reference` and set no `ai-input` signal, so their page text is not sent to Claude |
