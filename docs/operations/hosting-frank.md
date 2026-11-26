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
| `projects/hazelst/secrets.enc.env` | SOPS-encrypted `ANTHROPIC_API_KEY`; decrypted at deploy to git-ignored `hazelst.env` |
| `platform/Caddyfile` | `http://hazelst.thmxtp.com { reverse_proxy hazelst:8080 }` |
| `platform/cloudflared/config.yml` | Ingress rule `hostname: hazelst.thmxtp.com` → `http://caddy:80`, above the catch-all |
| `systemd/frank-hazelst.service` | Boot supervision, same shape as the other stacks |

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
    env_file: [hazelst.env]   # ANTHROPIC_API_KEY (decrypted from secrets.enc.env)
    environment:
      TZ: America/New_York
      NWS_USER_AGENT: hazelst-forecast
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

No published ports, no host network, plain HTTP on `edge` (TLS terminates at
Cloudflare, like `bcl-gateway`).

## Outbound traffic

The container needs HTTPS egress to: `api.open-meteo.com`, `api.weather.gov`,
`mesonet.agron.iastate.edu`, the NOHRSC data host, each enabled resort report site, and
`api.anthropic.com`. frank is dual-stack with no per-container egress filter
today; if one is added, this is the allowlist.

## Operations

- **Logs:** stdout, one line per job step with source timings and failures
  (picked up by the Phase 5 log pipeline when it lands).
- **Backups:** `/srv/hazelst/hazelst.db` is the product's memory (all raw data,
  verification, learned skill, knowledge). Nightly `sqlite3 .backup` into the
  frank backup set. Size < 1 GB per season.
- **Failure behavior:** a failed source degrades that section (noted in the page
  footer); a failed Claude call publishes the numbers-only page with "report
  delayed" and retries every 15 minutes until 9 AM.

## Defaults

Repo, image, hostname, exposure, storage (`lv-hazelst`, 10 GB at
`/srv/hazelst`), and the host-action bundle are in
[technical-design.md §17](../design/technical-design.md).
