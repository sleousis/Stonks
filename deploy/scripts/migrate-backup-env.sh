#!/usr/bin/env bash
# Move the backup secrets out of deploy/.env into deploy/backup.env.
#
# deploy/.env goes to every app container (api, scheduler, lab-worker).
# The restic password, the bucket keys and the host ping URLs belong in
# deploy/backup.env, which only the restic container and the host scripts
# read. This script moves each of those lines (the keys in BACKUP_ENV_KEYS,
# lib.sh) and leaves every other line alone. A key already set in
# backup.env keeps its value there, and the old line in .env is dropped.
# Safe to run twice.
#
# Usage (on the server):  deploy/scripts/migrate-backup-env.sh
# Then recreate the containers so they lose the old values:
#   cd deploy && docker compose up -d
set -Eeuo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC2034 # read by log() in lib.sh
LOG_TAG=migrate-backup-env
# shellcheck source=lib.sh
. "$DEPLOY_DIR/scripts/lib.sh"

env_file="$DEPLOY_DIR/.env"
backup_file="$DEPLOY_DIR/backup.env"
[ -f "$env_file" ] || {
	log "$env_file not found; nothing to move"
	exit 0
}
umask 077
touch "$backup_file"
chmod 600 "$backup_file"

moved=()
dropped=()
keep="$(mktemp "$DEPLOY_DIR/.env.migrate.XXXXXX")"
trap 'rm -f "$keep"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
	key="${line%%=*}"
	if [ "$key" != "$line" ] && [[ " $BACKUP_ENV_KEYS " == *" $key "* ]]; then
		if grep -qE "^${key}=" "$backup_file"; then
			dropped+=("$key")
		else
			printf '%s\n' "$line" >>"$backup_file"
			moved+=("$key")
		fi
		continue
	fi
	printf '%s\n' "$line" >>"$keep"
done <"$env_file"

# Rewrite .env in place, so its owner and mode stay as they were.
cat "$keep" >"$env_file"

if [ "${#moved[@]}" -gt 0 ]; then
	log "moved to backup.env: ${moved[*]}"
fi
if [ "${#dropped[@]}" -gt 0 ]; then
	log "already in backup.env, removed from .env: ${dropped[*]}"
fi
if [ "${#moved[@]}" -eq 0 ] && [ "${#dropped[@]}" -eq 0 ]; then
	log "nothing to move; .env holds no backup keys"
else
	log "done. Run 'docker compose up -d' in $DEPLOY_DIR so the app containers drop the old values"
fi
