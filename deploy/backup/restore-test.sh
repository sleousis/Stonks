#!/usr/bin/env bash
# Monthly automated restore test (Phase 14.5). Touches nothing live.
#
#   restic check (10% of the data) -> restore latest into the scratch `restore`
#   volume -> restore it into a scratch data folder with the app -> check the
#   state row counts, the lake and the artifacts -> wipe scratch
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
restic restore latest --host stonks --target /restore/snapshot

# Restore with the same tool as restore.sh into a scratch data folder, then
# check that the state DB holds the snapshot's users, strategies and orders
# rows and that the lake and artifacts came back. A missing or empty state DB
# fails the test. Runs as root: restic restores as root.
app_ops() {
	compose run --rm --no-deps -T --user 0 -v "stonks_restore:/restore" api \
		python -m stonks.ops "$@"
}
app_ops restore-snapshot /restore/snapshot --data-dir /restore/check
app_ops check-restore /restore/check --snapshot /restore/snapshot
log "restore test passed"
