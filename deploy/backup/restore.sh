#!/usr/bin/env bash
# Disaster restore from the off-server backup (Phase 14.5).
# Restores the state DB, the artifacts and the lake from a restic snapshot
# with `python -m stonks.ops restore-snapshot`. A local snapshot of the
# current data is taken first, and the current files are moved aside, not
# deleted.
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

clear_scratch() {
	compose --profile backup run --rm -T --entrypoint sh restic -c 'rm -rf /restore/* /restore/.[!.]*'
}
clear_scratch
restic restore "$snap" --host stonks --target /restore/snapshot

# The app restores the state DB, the artifacts and the lake together, checks
# the users, strategies and orders row counts, and moves what was in /data
# aside (<name>.pre-restore-<stamp>). It never deletes data, and it refuses a
# snapshot without the state DB or the lake before touching anything.
if ! compose run --rm --no-deps -T --user 0 -v "stonks_restore:/restore:ro" api \
	python -m stonks.ops restore-snapshot /restore/snapshot --data-dir /data; then
	log "restore refused or failed; /data is unchanged (restart with: docker compose up -d)"
	exit 1
fi
docker run --rm -v "$(data_path):/data" "$ALPINE_IMAGE" chown -R 10001:10001 /data
clear_scratch

compose up -d
wait_healthy 180
notify warning "data restored from restic snapshot $snap"
