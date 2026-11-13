#!/usr/bin/env bash
# Blue/green deployment of the fictional demo only. No access to the real bot.
set -Eeuo pipefail
umask 077
SCRIPT_DIR=$(cd "$(dirname "$0")/../deploy" && pwd)
IMAGE=${1:?Usage: scripts/deploy_demo.sh IMAGE}
[[ "$IMAGE" =~ ^[a-zA-Z0-9._/:@-]+$ ]] || { echo 'Invalid image reference'; exit 2; }
export DEPLOY_STATE_DIR=${DEPLOY_STATE_DIR:-"$SCRIPT_DIR/.state"}
export DEMO_PORT=${DEMO_PORT:-8080}
PROJECT=${DEMO_PROJECT:-innosport-demo}
HEALTH_TIMEOUT=${DEMO_HEALTH_TIMEOUT:-60}
[[ "$DEMO_PORT" =~ ^[0-9]+$ && "$HEALTH_TIMEOUT" =~ ^[0-9]+$ ]] || exit 2
ENV_FILE=${DEMO_ENV_FILE:-"$SCRIPT_DIR/.env"}
mkdir -p "$DEPLOY_STATE_DIR/nginx"
mkdir "$DEPLOY_STATE_DIR/deploy.lock" 2>/dev/null || { echo 'Another deployment is running'; exit 1; }
old_slot= old_image= candidate= promoted=0 committed=0

compose() { docker compose --env-file "$ENV_FILE" -p "$PROJECT" -f "$SCRIPT_DIR/compose.demo.yml" "$@"; }

finish() {
  status=$?
  trap - EXIT
  if [[ $status -ne 0 && $committed -eq 0 && -n "$candidate" ]]; then
    echo 'Candidate failed; restoring the previous release.' >&2
    if [[ -n "$old_slot" ]]; then
      if [[ $promoted -eq 1 ]]; then
        cp "$DEPLOY_STATE_DIR/nginx.previous" "$DEPLOY_STATE_DIR/nginx/default.conf"
        compose up -d --no-deps "$old_slot" >/dev/null 2>&1 || true
        if ! compose exec -T gateway nginx -t || ! compose exec -T gateway nginx -s reload; then
          echo 'ROLLBACK FAILED: gateway needs operator attention.' >&2
        elif ! python3 "$SCRIPT_DIR/probe.py" "http://127.0.0.1:$DEMO_PORT"; then
          echo 'ROLLBACK FAILED: previous release did not pass the smoke check.' >&2
        fi
      fi
    elif [[ $promoted -eq 1 ]]; then
      compose stop gateway >/dev/null 2>&1 || true
    fi
    compose stop "$candidate" >/dev/null 2>&1 || true
  fi
  rmdir "$DEPLOY_STATE_DIR/deploy.lock" 2>/dev/null || true
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -f "$ENV_FILE" ]]; then
  python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import secrets
import sys
path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
with path.open('x') as stream:
    stream.write('DEMO_DB_PASSWORD=' + secrets.token_hex(32) + '\n')
path.chmod(0o600)
PY
fi
if [[ -f "$DEPLOY_STATE_DIR/active" ]]; then
  read -r old_slot old_image < "$DEPLOY_STATE_DIR/active"
  [[ "$old_slot" == blue || "$old_slot" == green ]] || { echo 'Invalid release state'; exit 1; }
  [[ "$old_image" =~ ^[a-zA-Z0-9._/:@-]+$ ]] || exit 2
  [[ -f "$DEPLOY_STATE_DIR/nginx/default.conf" ]] || { echo 'Missing active gateway configuration'; exit 1; }
fi
candidate=blue
[[ "$old_slot" != blue ]] || candidate=green
export BLUE_IMAGE=$IMAGE GREEN_IMAGE=$IMAGE
if [[ "$old_slot" == blue ]]; then export BLUE_IMAGE=$old_image; fi
if [[ "$old_slot" == green ]]; then export GREEN_IMAGE=$old_image; fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then docker pull "$IMAGE"; fi
EXPECTED=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")
[[ -n "$EXPECTED" && "$EXPECTED" != '<no value>' ]] || { echo 'Image lacks a release revision'; exit 1; }
compose up -d --wait db
compose up -d --no-deps "$candidate"
ready=0
deadline=$((SECONDS + HEALTH_TIMEOUT))
while [[ $SECONDS -lt $deadline ]]; do
  if compose exec -T "$candidate" python -c \
    "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/health/ready', timeout=3).read()" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[[ $ready -eq 1 ]] || { echo 'Candidate readiness check failed'; exit 1; }
if [[ -n "$old_slot" ]]; then cp "$DEPLOY_STATE_DIR/nginx/default.conf" "$DEPLOY_STATE_DIR/nginx.previous"; fi
cat > "$DEPLOY_STATE_DIR/nginx/next.conf.tmp" <<EOF
server {
    listen 8080;
    server_name _;
    location / {
        proxy_pass http://$candidate:8080;
        proxy_connect_timeout 3s;
        proxy_read_timeout 10s;
    }
}
EOF
promoted=1
mv "$DEPLOY_STATE_DIR/nginx/next.conf.tmp" "$DEPLOY_STATE_DIR/nginx/default.conf"
compose up -d --no-deps gateway
compose exec -T gateway nginx -t
compose exec -T gateway nginx -s reload
smoke=0
for attempt in 1 2 3 4 5; do
  if python3 "${DEMO_SMOKE_SCRIPT:-$SCRIPT_DIR/probe.py}" "http://127.0.0.1:$DEMO_PORT" "$EXPECTED"; then
    smoke=1
    break
  fi
  sleep 1
done
[[ $smoke -eq 1 ]] || { echo 'Release smoke check failed'; exit 1; }
printf '%s %s\n' "$candidate" "$IMAGE" > "$DEPLOY_STATE_DIR/active.next"
mv "$DEPLOY_STATE_DIR/active.next" "$DEPLOY_STATE_DIR/active"
committed=1
echo "Release promoted: $candidate ($EXPECTED). Previous slot retained for rollback."
