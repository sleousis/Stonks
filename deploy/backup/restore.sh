#!/usr/bin/env bash
# Disaster restore from the off-server backup (Phase 14.5).
# Replaces the live data volume with a restic snapshot. A local snapshot of
# the current data is taken first.
#
# Usage: deploy/backup/restore.sh [restic-snapshot-id]   (default: latest)
#        restic snapshots:  docker compose --profile backup run --rm restic snapshots
set -Eeuo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC2034 # read by log() in lib.sh
LOG_TAG=restore
# shellcheck source=../scripts/lib.sh
. "$DEPLOY_DIR/scripts/lib.sh"
cd "$DEPLOY_DIR"

snap="${1:-latest}"
restic() { compose --profile backup run --rm -T restic "$@"; }

read -r -p "Replace ALL live data with restic snapshot '$snap'? Type RESTORE: " answer
[ "$answer" = "RESTORE" ] || {
	log "aborted"
	exit 1
}

stop_writers
snapshot_create "before-restore-$(date -u +%Y%m%dT%H%M%SZ)"

compose --profile backup run --rm -T --entrypoint sh restic -c 'rm -rf /restore/* /restore/.[!.]*'
restic restore "$snap" --host stonks --target /restore

lake="$(compose --profile backup run --rm -T --entrypoint sh restic -c \
	'find /restore -name lake.duckdb -exec dirname {} \; | sort -r | head -n 1')"
lake="$(printf '%s' "$lake" | tr -d '\r')"
[ -n "$lake" ] || {
	log "no lake.duckdb in snapshot $snap; nothing changed (restart with: docker compose up -d)"
	exit 1
}
log "copying $lake into the data volume"
docker run --rm -v stonks_restore:/restore:ro -v "$(data_path):/data" "$ALPINE_IMAGE" sh -c \
	"find /data -mindepth 1 -maxdepth 1 ! -name backups -exec rm -rf {} + && cp -a '$lake'/. /data/ && chown -R 10001:10001 /data"
compose --profile backup run --rm -T --entrypoint sh restic -c 'rm -rf /restore/* /restore/.[!.]*'

compose up -d
wait_healthy 180
notify warning "data restored from restic snapshot $snap"
