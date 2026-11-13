#!/usr/bin/env bash
# Real container test: two releases, failed readiness, and failure after switching traffic.
set -euo pipefail
IMAGE=${1:?Usage: scripts/test_deployment.sh IMAGE}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
export DEMO_PROJECT="innosport-ci-$$"
mkdir -p "$HOME/.local/share"
export DEPLOY_STATE_DIR=$(mktemp -d "$HOME/.local/share/innosport-ci.XXXXXX")
export DEMO_ENV_FILE="$DEPLOY_STATE_DIR/demo.env"
export DEMO_PORT=${DEMO_TEST_PORT:-18080}
export DEMO_HEALTH_TIMEOUT=12
mkdir -p "$DEPLOY_STATE_DIR"
cleanup() {
  BLUE_IMAGE="$IMAGE" GREEN_IMAGE="$IMAGE" docker compose --env-file "$DEMO_ENV_FILE" -p "$DEMO_PROJECT" \
    -f deploy/compose.demo.yml down -v --remove-orphans || true
  rm -rf "$DEPLOY_STATE_DIR"
  docker image rm "innosport-broken:$$" "innosport-candidate:$$" >/dev/null 2>&1 || true
}
trap cleanup EXIT
bash scripts/deploy_demo.sh "$IMAGE"
first=$(cat "$DEPLOY_STATE_DIR/active")
first_revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")
second_revision="$first_revision-candidate"
docker build -t "innosport-candidate:$$" -f - . <<EOF
FROM $IMAGE
ENV APP_REVISION=$second_revision
LABEL org.opencontainers.image.revision=$second_revision
EOF
bash scripts/deploy_demo.sh "innosport-candidate:$$"
second=$(cat "$DEPLOY_STATE_DIR/active")
[[ "$first" != "$second" ]]
docker build -t "innosport-broken:$$" -f - . <<EOF
FROM $IMAGE
CMD ["python", "-c", "import time; time.sleep(300)"]
EOF
if bash scripts/deploy_demo.sh "innosport-broken:$$"; then echo 'Broken image was promoted'; exit 1; fi
[[ "$(cat "$DEPLOY_STATE_DIR/active")" == "$second" ]]
python3 deploy/probe.py "http://127.0.0.1:$DEMO_PORT" "$second_revision"
printf 'raise SystemExit(1)\n' > "$DEPLOY_STATE_DIR/fail_smoke.py"
if DEMO_SMOKE_SCRIPT="$DEPLOY_STATE_DIR/fail_smoke.py" bash scripts/deploy_demo.sh "$IMAGE"; then
  echo 'Release with failing smoke check was promoted'; exit 1
fi
[[ "$(cat "$DEPLOY_STATE_DIR/active")" == "$second" ]]
python3 deploy/probe.py "http://127.0.0.1:$DEMO_PORT" "$second_revision"
echo 'Blue/green promotion and both rollback paths passed.'
