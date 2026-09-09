#!/bin/sh
# Writes {container: state} next to the map's data, for the live layer.
#
# Run it from cron on the Docker host, every minute:
#   * * * * * /path/to/netmap/nas-docker-state.sh
#
# It exists so the map never needs /var/run/docker.sock mounted into a
# container that sits behind no authentication — that would hand out
# root-equivalent control of the host. Containers do not change state
# second-to-second, so a one-minute-old file is just as good.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT="${NETMAP_DOCKER_JSON:-$DIR/data/docker.json}"
mkdir -p "$(dirname "$OUT")"
docker ps -a --format '{{.Names}}\t{{.State}}' 2>/dev/null | awk -F'\t' '
  BEGIN { printf "{" ; sep="" }
  NF==2 { printf "%s\"%s\":\"%s\"", sep, $1, $2 ; sep="," }
  END   { print "}" }' > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
