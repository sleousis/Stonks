#!/usr/bin/env bash
# Nightly encrypted off-server backup with restic (Phase 14.5).
#
# What gets backed up:
#   1. If the image has `python -m stonks.ops backup` (Phase 12.4), it writes a
#      consistent copy of the lake, Parquet bars, state and artifacts into
#      /data/backups, and restic uploads that folder. No downtime.
#   2. Otherwise the writers (api, scheduler, lab-worker) are stopped for the
#      upload, restic copies the whole data volume, and they start again.
#
# Usage: deploy/backup/backup.sh [--services-stopped] [--tag TAG]
# Needs RESTIC_REPOSITORY, RESTIC_PASSWORD and the S3 keys in deploy/.env.
set -Eeuo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC2034 # read by log() in lib.sh
LOG_TAG=backup
# shellcheck source=../scripts/lib.sh
. "$DEPLOY_DIR/scripts/lib.sh"
cd "$DEPLOY_DIR"

services_stopped=0
tags=(--tag nightly)
while [ $# -gt 0 ]; do
	case "$1" in
	--services-stopped) services_stopped=1 ;;
	--tag)
		tags=(--tag "$2")
		shift
		;;
	*)
		log "unknown argument $1"
		exit 2
		;;
	esac
	shift
done

hc="$(env_get HC_PING_BACKUP)"
[ -n "$(env_get RESTIC_REPOSITORY)" ] || {
	log "RESTIC_REPOSITORY not set; skipping"
	exit 0
}
ping_hc "$hc" start

restarted=0
finish() {
	local rc=$?
	if [ "$restarted" = 1 ]; then
		start_writers || notify error "backup could not restart the api and the other writers"
	fi
	if [ "$rc" = 0 ]; then
		ping_hc "$hc"
	else
		ping_hc "$hc" fail
		notify error "off-server backup failed (exit $rc)"
	fi
}
trap finish EXIT

restic() { compose --profile backup run --rm -T restic "$@"; }

# Create the repository on first use.
if ! restic cat config >/dev/null 2>&1; then
	log "initialising restic repository"
	restic init
fi

backup_cmd="$(env_get STONKS_BACKUP_CMD "python -m stonks.ops backup")"
app_backup=0
if [ "$services_stopped" = 0 ] &&
	compose exec -T api python -c "import stonks.ops" >/dev/null 2>&1; then
	log "app backup: $backup_cmd"
	# shellcheck disable=SC2086 # the command is meant to word-split
	if compose exec -T api $backup_cmd; then
		app_backup=1
	else
		log "app backup failed; falling back to a stopped-volume copy"
	fi
fi

if [ "$app_backup" = 1 ]; then
	restic backup "${tags[@]}" --host stonks /data/backups
else
	if [ "$services_stopped" = 0 ]; then
		stop_writers
		restarted=1
	fi
	restic backup "${tags[@]}" --host stonks --exclude /data/backups /data
fi

# Retention: 7 daily, 4 weekly, 12 monthly.
restic forget --host stonks --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
log "done"
