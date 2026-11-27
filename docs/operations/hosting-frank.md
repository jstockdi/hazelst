# Hosting on frank

frank runs each project as a digest-pinned Compose stack behind Cloudflare
Tunnel → Caddy (frank repo: `docs/02-stack-and-architecture.md`,
`docs/03-release-and-operations.md`, `projects/README.md`). Hazel St Forecast
follows the same contract as `mixtape` and `bcl`.

## Release

1. `image.yml` in this repo builds on push to `main` and pushes
   `ghcr.io/jstockdi/hazelst:<git-sha>` — `amd64` only, no `latest` tag.
2. The deploy pins the **digest** of that SHA in the frank repo's
   `projects/hazelst/compose.yml`, in a PR against frank.
3. On frank: `make deploy STACK=hazelst` (pull the pinned digest, `up -d`).
4. Rollback: revert the digest in `compose.yml`, deploy again. The SQLite file
   is forward-compatible across one release (migrations are additive only).

## Changes in the frank repo

| File | Change |
|------|--------|
| `projects/hazelst/compose.yml` | New stack (below) |
| `projects/hazelst/secrets.enc.env` | SOPS-encrypted `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`); decrypted at deploy to git-ignored `hazelst.env` (mode 600) |
| `platform/Caddyfile` | `http://hazelst.thmxtp.com { reverse_proxy hazelst:8080 }` |
| `platform/cloudflared/config.yml` | Ingress rule `hostname: hazelst.thmxtp.com` → `http://caddy:80`, above the catch-all |
| `systemd/frank-hazelst.service` | Boot supervision, same shape as the other stacks |
| `scripts/hazelst-storage.sh` | Idempotent `lv-hazelst` carve, fstab mount, owner and mode |
| `projects/monitoring/prometheus/prometheus.yml` | Scrape job `hazelst` → `hazelst:8080/metrics` over `edge` |
| `projects/monitoring/prometheus/rules/hazelst.rules.yml` | Report-age, backup-age, and scrape-down alerts |

Merged as frank !68 (stack), !69 (ingress), and !70 (monitoring and the
J20 digest).

## Compose sketch

```yaml
name: frank-hazelst

networks:
  edge:
    external: true

services:
  hazelst:
    image: ghcr.io/jstockdi/hazelst@sha256:<digest> # <git-sha>
    container_name: hazelst
    restart: unless-stopped
    networks: [edge]          # Caddy reaches hazelst:8080; outbound HTTPS for data sources
    env_file: [hazelst.env]   # CLAUDE_CODE_OAUTH_TOKEN (decrypted from secrets.enc.env)
    environment:
      TZ: America/New_York
      NWS_USER_AGENT: hazelst-forecast (github.com/jstockdi/hazelst)
      HOME: /tmp/home         # writable HOME on the tmpfs for the Claude CLI
      HAZELST_SCHEDULER: "true"  # run the job schedule in the serving process
      HAZELST_ENABLE_AGENTS: "true"  # Claude calls, bounded by the daily cap (10)
    volumes:
      - /srv/hazelst:/data:rw # SQLite; host dir pre-chowned 65532:65532, 0750
    user: "65532:65532"
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    healthcheck:
      start_period: 30s       # image ships HEALTHCHECK -> GET /health
    deploy:
      resources:
        limits: { cpus: "1.0", memory: 512M }
```

`ANTHROPIC_API_KEY` must not appear in `hazelst.env` or `environment`: the
Claude CLI prefers it over the token. The Claude CLI writes ~32 KB of config
files under `HOME` per call; `/tmp` is a tmpfs, so nothing persists and the root
filesystem stays read-only (verified in the J01 spike).

No published ports, no host network, plain HTTP on `edge` (TLS terminates at
Cloudflare, like `bcl-gateway`).

## Outbound traffic

The container needs HTTPS egress to: `api.open-meteo.com`,
`previous-runs-api.open-meteo.com` (`season-backfill` only), `api.weather.gov`,
`mesonet.agron.iastate.edu`, the NOHRSC data host, `good-cormorant-17.convex.site`
and `snocountry.com` (SnoCountry), `www.ncei.noaa.gov` (GHCN-Daily), each enabled
resort report site, and
`api.anthropic.com` (Claude Agent SDK; nonessential CLI traffic such as
telemetry and update checks is disabled). frank is dual-stack with no per-container egress filter
today; if one is added, this is the allowlist.

## Operations

- **Logs:** stdout, one line per job step with source timings and failures
  (picked up by the Phase 5 log pipeline when it lands).
- **Backups:** `/srv/hazelst/hazelst.db` is the product's memory (all raw data,
  verification, learned skill, knowledge). The `backup` job runs at 4:00 AM in the
  in-process schedule: Python's `sqlite3` online backup API (frank has no
  `sqlite3` CLI) writes `/srv/hazelst/backups/hazelst-<date>.db` through a
  `.partial` file and a rename, and keeps the newest 14. Run one by hand with
  `docker exec hazelst hazelst run backup`. The copies share the LV with the live
  database; an off-host copy belongs to the frank backup set (frank doc 03 §7),
  not yet built. Size < 1 GB per season.
- **Alerts:** `/metrics` exports `hazelst_report_published_timestamp_seconds`
  and `hazelst_backup_last_success_timestamp_seconds`, read from the database at
  scrape time. frank's Prometheus (frank-monitoring, on `edge`) scrapes
  `hazelst:8080` and evaluates `rules/hazelst.rules.yml`:
  `HazelstReportStale` (newest report older than 26 h for 15 min),
  `HazelstBackupStale` (newest backup older than 26 h for 30 min), and
  `HazelstScrapeDown` (`up == 0` for 10 min). Like frank's other rules they
  surface in Prometheus `/alerts` and Grafana; no Alertmanager pages yet.
- **Season backfill** (manual, once after the first deploy): loads the
  2025–26 season and rebuilds `skill`:

  ```bash
  docker exec hazelst hazelst season-backfill --from 2025-11-01 --to 2026-04-30
  ```

  It runs one `season-backfill` job per month (the job lock is released between
  months, so scheduled jobs wait at most one month's run), then `skill-learn`,
  and prints one JSON line per month and a final summary (`seconds`,
  `db_bytes`, the `skill-learn` summary). Measured locally on 2026-11-26: a
  full month took 67 s (Previous Runs 32 s, PNS 22 s, NOHRSC 12 s, LSR 1 s)
  and added 30 MB, so the season takes about 7 minutes and about 180 MB
  (NOHRSC grids are half of it). Run it outside the 6:45–9:00 AM and
  2:30–2:45 PM slots. It is idempotent: a rerun sends only the requests that
  failed before, so after an error the same command fills the gaps.

  **SnoCountry history** (J23, once after its deploy): one request per
  mountain loads the whole resort history, then the stored editions are
  re-scored and `skill` is rebuilt:

  ```bash
  docker exec hazelst hazelst season-backfill --from 2019-11-01 --to 2026-04-30 --sources snocountry --no-learn
  docker exec hazelst hazelst forecast-verify --from 2024-01-20 --to 2026-04-30
  docker exec hazelst hazelst run skill-learn
  ```

  `--sources` takes any subset of `previous_runs,lsr,pns,nohrsc,snocountry,ghcn`.
  With `snocountry` alone it is one job run (about 2 s, 15,700 rows); a rerun
  sends no requests.

  **GHCN-Daily station history** (J24, once after its deploy):

  ```bash
  docker exec hazelst hazelst season-backfill --from 2024-01-20 --to 2026-04-30 --sources ghcn --no-learn
  docker exec hazelst hazelst forecast-verify --from 2024-01-20 --to 2026-04-30
  docker exec hazelst hazelst run skill-learn
  docker exec hazelst hazelst stations summary --season 2025
  ```

  Measured locally on 2026-11-26: 3 requests and about 2 s per month (132
  stations in batches of 50), about 2,200 `snow_report` rows per winter month.
- **Report elevations** (J25, manual, once after the J25 deploy): fill
  `snow_report.elev_ft` for rows stored before J25, re-score the backfilled
  range, and read the scores by observation elevation:

  ```bash
  docker exec hazelst hazelst elevations fill
  docker exec hazelst hazelst forecast-verify --from 2024-01-20 --to 2026-04-30
  docker exec hazelst hazelst verify summary --by elevation
  ```

  `elevations fill` prints the rows filled and the rows still without
  `elev_ft` (failed lookups; a rerun retries them). Days with no stored report
  are scored as edition `backfill` from the stored Previous Runs forecasts.
  `verify summary` takes `--from`, `--to`, `--edition`, `--method`, and
  `--all-days` (default: snow days only, earliest edition per day).
- **Failure behavior:** a failed source degrades that section (noted in the page
  footer); a failed Claude call publishes the numbers-only page with "report
  delayed" and retries every 15 minutes until 9 AM.

## Defaults

Repo, image, hostname, exposure, storage (`lv-hazelst`, 10 GB at
`/srv/hazelst`), and the host-action bundle are in
[technical-design.md §17](../design/technical-design.md).

## Runbook

All commands run on frank in `/opt/frank` (branch `main`).

**First deploy** (done 2026-11-26, J18):

```bash
sudo scripts/hazelst-storage.sh                    # lv-hazelst 10 GB at /srv/hazelst, 65532:65532, 0750
(umask 077; sops -d projects/hazelst/secrets.enc.env > projects/hazelst/hazelst.env)
make deploy STACK=hazelst
sudo cp systemd/frank-hazelst.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now frank-hazelst.service
docker exec frank-caddy wget -qO- http://hazelst:8080/health   # {"status":"ok",...}
```

**Ingress** (frank !69): after `git pull`, `docker compose -f platform/compose.yml
restart caddy cloudflared` re-reads the Caddyfile and tunnel config (single-file
mounts; Caddy's admin API is off). `play.` and `bcl.` blip for a few seconds.
The DNS record is a proxied CNAME `hazelst` →
`a2ee0885-c89b-457e-a27b-2f80d3348eba.cfargotunnel.com` in the `thmxtp.com`
zone. Neither frank nor the Mac holds a Cloudflare login, so the record is
added in the Cloudflare dashboard (as for `bcl.`). Until it exists, Cloudflare
answers the hostname with 530.

**Prometheus config change** without restarting the monitoring stack:
`prometheus.yml` is a single-file bind mount, and `git pull` replaces the file
with a new inode that the running container never sees. Write the new content
into the existing file first, stage it, then pull and hot-reload:

```bash
git fetch
git show origin/main:projects/monitoring/prometheus/prometheus.yml > projects/monitoring/prometheus/prometheus.yml
git add projects/monitoring/prometheus/prometheus.yml
git pull --ff-only
curl -s -X POST http://127.0.0.1:9090/-/reload
```

New rule files appear through the `rules/` directory mount, so the reload
picks them up.

**Upgrade:** a frank PR bumps the digest in `projects/hazelst/compose.yml`
(the trailing comment names the git SHA and the rollback digest); merge,
`git pull`, `make deploy STACK=hazelst`. The scheduler catches up at startup:
when the container starts after an edition time and that edition is missing,
it ingests and publishes it.

**Rollback:** pin the previous digest and deploy. In an incident, edit the
digest on frank, `make deploy STACK=hazelst`, and follow with a frank PR (or
`git restore` the file once fixed forward).

Rehearsed 2026-11-26 from `a4e825c` to the previous digest `f5e5dd8` and back:
each deploy reached `healthy` in about 20 s and `/health` returned 200. The
rolled-back page returned 500, because `f5e5dd8` rejects the payload fields
that `a4e825c` added (`extra_forbidden`). Since J20 the page reads stored
payloads with unknown fields dropped, so a rollback to a J20-or-later image
renders rows written by a newer one. The database needs nothing: migrations
are additive.
