#!/usr/bin/env bash
# Smoke test a built image under the production container constraints:
# read-only rootfs, tmpfs /tmp, uid 65532, no capabilities, 1 CPU / 512 MB.
# Usage: scripts/smoke.sh <image>
set -euo pipefail

image="${1:?usage: scripts/smoke.sh <image>}"
name="hazelst-smoke-$$"
port="${SMOKE_PORT:-18080}"

cleanup() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    echo "smoke: failed; container logs:" >&2
    docker logs "$name" >&2 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$name" \
  --read-only --tmpfs /tmp \
  --tmpfs /data:uid=65532,gid=65532,mode=0750 \
  --user 65532:65532 --cap-drop ALL --security-opt no-new-privileges:true \
  --cpus 1 --memory 512m \
  -p "127.0.0.1:${port}:8080" \
  "$image" >/dev/null

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "health: $(curl -fsS "http://127.0.0.1:${port}/health")"
curl -fsS "http://127.0.0.1:${port}/metrics" | grep '^hazelst_build_info'
docker exec "$name" hazelst healthcheck
docker exec "$name" hazelst migrate
docker exec "$name" python -c "import zoneinfo; print('tz:', zoneinfo.ZoneInfo('America/New_York'))"
test "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$name")" = true
echo "rootfs: read-only"
echo "user: $(docker exec "$name" id -u):$(docker exec "$name" id -g)"
echo "smoke: ok"
