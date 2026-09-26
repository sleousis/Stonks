#!/usr/bin/env bash
# Monthly automated restore test (Phase 14.5). Touches nothing live.
#
#   restic check (10% of the data) -> restore latest into the scratch `restore`
#   volume -> open the restored stores with `stonks db info` -> wipe scratch
#
# Usage: deploy/backup/restore-test.sh
set -Eeuo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC2034 # read by log() in lib.sh
LOG_TAG=restore-test
# shellcheck source=../scripts/lib.sh
. "$DEPLOY_DIR/scripts/lib.sh"
cd "$DEPLOY_DIR"

hc="$(env_get HC_PING_RESTORE_TEST)"
ping_hc "$hc" start
finish() {
	local rc=$?
	compose --profile backup run --rm -T --entrypoint sh restic -c 'rm -rf /restore/* /restore/.[!.]*' >/dev/null 2>&1 || true
	if [ "$rc" = 0 ]; then
		ping_hc "$hc"
	else
		ping_hc "$hc" fail
		notify error "monthly restore test FAILED (exit $rc); backups may be unusable"
	fi
}
trap finish EXIT

restic() { compose --profile backup run --rm -T restic "$@"; }

restic check --read-data-subset=10%
compose --profile backup run --rm -T --entrypoint sh restic -c 'rm -rf /restore/* /restore/.[!.]*'
restic restore latest --host stonks --target /restore

# Find the restored lake (stopped-volume copies keep /data/..., app backups
# keep /data/backups/...). The last one by path (newest timestamped folder) wins.
lake="$(compose --profile backup run --rm -T --entrypoint sh restic -c \
	'find /restore -name lake.duckdb -exec dirname {} \; | sort -r | head -n 1')"
lake="$(printf '%s' "$lake" | tr -d '\r')"
[ -n "$lake" ] || {
	log "no lake.duckdb in the restored snapshot"
	exit 1
}
log "restored lake folder: $lake"

# Open both stores with the app itself (as root: restic restores as root).
compose run --rm --no-deps -T --user 0 -v "stonks_restore:/restore" \
	-e STONKS_DATA_DIR="$lake" api stonks db info
log "restore test passed"
