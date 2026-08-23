#!/usr/bin/env bash
#
# dockerRun.sh — start the board server. Plain docker run, no compose.
#
# The image holds the server and nothing else. Boards stay on this host and
# are mounted read-only, so re-exporting is a file operation: no rebuild, no
# restart, nothing to do here afterwards.
#
#   ./dockerRun.sh                 start it
#   ./dockerRun.sh --rebuild       rebuild the image first
#   ./dockerRun.sh --foreground    run attached, logs on the terminal, ^C stops
#   ./dockerRun.sh --stop          stop and remove the container
#
# Settings below can be overridden by a .env file or by the environment:
#   BOARDS_DIR=/mnt/boards PORT=9000 ./dockerRun.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# ---- settings --------------------------------------------------------------

CONFIG_VARS="BOARDS_DIR BIND_ADDR PORT IMAGE CONTAINER FREEFORM_AUTH FREEFORM_ASSET_MAX_AGE"
ENV_OVERRIDES="$(export -p | grep -E "^(declare -x |export )($(echo $CONFIG_VARS | tr ' ' '|'))=" || true)"

# Where the export lives on this host.
BOARDS_DIR="./boards"
# 127.0.0.1 keeps it on this machine. 0.0.0.0 opens it to the network.
BIND_ADDR="127.0.0.1"
PORT="9384"
IMAGE="freeform-boards"
CONTAINER="freeform-boards"
# user:password to require basic auth, blank for none.
FREEFORM_AUTH=""
# Seconds a browser may reuse images and video. Pages always revalidate.
FREEFORM_ASSET_MAX_AGE="300"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
eval "$ENV_OVERRIDES"

case "$BOARDS_DIR" in
  /*) ABS_BOARDS="$BOARDS_DIR" ;;
  *)  ABS_BOARDS="$HERE/${BOARDS_DIR#./}" ;;
esac

die() { printf '\033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

REBUILD=0
FOREGROUND=0
STOP=0
for arg in "$@"; do
  case "$arg" in
    --rebuild)    REBUILD=1 ;;
    --foreground|--fg) FOREGROUND=1 ;;
    --stop|--down) STOP=1 ;;
    -h|--help)    awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

# ---- checks ----------------------------------------------------------------

command -v docker >/dev/null 2>&1 || die "docker is not installed."
docker info >/dev/null 2>&1 || die "the docker daemon is not reachable.
  Colima:         colima start
  Docker Desktop: open -a Docker"

if [ "$STOP" -eq 1 ]; then
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  echo "stopped $CONTAINER"
  exit 0
fi

[ -d "$ABS_BOARDS" ] || die "no export at $ABS_BOARDS
  Make one:  ./boardctl refresh
  Or point BOARDS_DIR at an export copied from another machine."

# ---- build -----------------------------------------------------------------

if [ "$REBUILD" -eq 1 ] || ! docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then
  # A root-owned ~/.docker/buildx, left behind by some past `sudo docker`,
  # makes buildx refuse to start. Nothing here needs it.
  if [ -d "$HOME/.docker/buildx" ] && [ ! -r "$HOME/.docker/buildx" ]; then
    export BUILDX_CONFIG="${BUILDX_CONFIG:-$HERE/.buildx}"
  fi
  echo "Building $IMAGE:latest"
  docker build --tag "$IMAGE:latest" --file Dockerfile .
fi

# ---- run -------------------------------------------------------------------

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# Hardening, line by line:
#   --read-only        the container filesystem cannot be written at all
#   --tmpfs /tmp       the one writable spot, and it cannot hold executables
#   --cap-drop ALL     no capabilities; nothing here needs one
#   --security-opt     no path to gaining privileges through setuid binaries
#   --user 10001       unprivileged, matching the user built into the image
#   --pids-limit       a runaway cannot fork the host into the ground
#   --memory           likewise for memory
#   :ro on the mount   the boards are readable and nothing more
set -- \
  --name "$CONTAINER" \
  --init \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user 10001:10001 \
  --pids-limit 128 \
  --memory 512m \
  --publish "$BIND_ADDR:$PORT:8080" \
  --volume "$ABS_BOARDS:/srv/boards:ro" \
  --env FREEFORM_AUTH="$FREEFORM_AUTH" \
  --env FREEFORM_ASSET_MAX_AGE="$FREEFORM_ASSET_MAX_AGE"

SHOWN="$BIND_ADDR"
[ "$SHOWN" = "0.0.0.0" ] && SHOWN="$(ipconfig getifaddr en0 2>/dev/null || hostname -f 2>/dev/null || echo localhost)"

if [ "$FOREGROUND" -eq 1 ]; then
  echo "Serving $ABS_BOARDS on http://$SHOWN:$PORT/   (^C to stop)"
  exec docker run --rm -it "$@" "$IMAGE:latest"
fi

docker run --detach --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=3 \
  "$@" "$IMAGE:latest" >/dev/null

# ---- wait for it to answer -------------------------------------------------

probe="$BIND_ADDR"
[ "$probe" = "0.0.0.0" ] && probe="127.0.0.1"
for _ in $(seq 1 50); do
  if curl -fsS -o /dev/null --max-time 2 "http://$probe:$PORT/healthz" 2>/dev/null; then
    echo "Serving $ABS_BOARDS"
    echo "  http://$SHOWN:$PORT/"
    [ -n "$FREEFORM_AUTH" ] && echo "  basic auth is on"
    echo "  logs:  docker logs -f $CONTAINER"
    echo "  stop:  ./dockerRun.sh --stop"
    exit 0
  fi
  state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null | tr -d '[:space:]' || true)"
  if [ "$state" = "exited" ]; then
    docker logs --tail 20 "$CONTAINER" >&2
    die "the container exited on startup."
  fi
  sleep 0.2
done
die "no answer on http://$probe:$PORT/healthz — try: docker logs $CONTAINER"
