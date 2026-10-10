#!/usr/bin/env bash
# Move the backup secrets out of deploy/.env into deploy/backup.env.
#
# deploy/.env goes to every app container (api, scheduler, lab-worker).
# The restic password, the bucket keys and the host ping URLs belong in
# deploy/backup.env, which only the restic container and the host scripts
# read. This script moves each of those lines (the keys in BACKUP_ENV_KEYS,
# lib.sh) and leaves every other line alone. Safe to run twice.
#
# backup.env may be a copy of backup.env.example: an empty or placeholder
# line there takes the value from .env. A key with a real value in both
# files is dropped from .env only when the two values match. When they
# differ, both stay, the script names the key and exits 1, so the only
# copy of a secret is never deleted.
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

# Put LINE in place of the KEY lines in backup.env (the first one), or
# append it when there is none. Rewritten in place, so the mode stays 600.
backup_put() {
	local key="$1" line="$2" tmp
	if grep -qE "^${key}=" "$backup_file"; then
		tmp="$(mktemp "$DEPLOY_DIR/.backup.env.migrate.XXXXXX")"
		KEY="$key" LINE="$line" awk '
			index($0, ENVIRON["KEY"] "=") == 1 { if (!done) print ENVIRON["LINE"]; done = 1; next }
			{ print }
		' "$backup_file" >"$tmp"
		cat "$tmp" >"$backup_file"
		rm -f "$tmp"
	else
		printf '%s\n' "$line" >>"$backup_file"
	fi
}

moved=()
dropped=()
conflicts=()
keep="$(mktemp "$DEPLOY_DIR/.env.migrate.XXXXXX")"
trap 'rm -f "$keep"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
	key="${line%%=*}"
	if [ "$key" != "$line" ] && [[ " $BACKUP_ENV_KEYS " == *" $key "* ]]; then
		value="$(env_unquote "${line#*=}")"
		if ! backup_env_has "$key"; then
			backup_put "$key" "$line"
			moved+=("$key")
		elif backup_placeholder "$value" ||
			[ "$value" = "$(file_get "$backup_file" "$key")" ]; then
			dropped+=("$key")
		else
			printf '%s\n' "$line" >>"$keep"
			conflicts+=("$key")
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
	log "same or empty value already in backup.env, removed from .env: ${dropped[*]}"
fi
if [ "${#conflicts[@]}" -gt 0 ]; then
	log "error: ${conflicts[*]} differ between .env and backup.env, so both were kept. Decide which value is right, put it in backup.env, delete the line from .env, then run this again"
	exit 1
fi
if [ "${#moved[@]}" -eq 0 ] && [ "${#dropped[@]}" -eq 0 ]; then
	log "nothing to move; .env holds no backup keys"
else
	log "done. Run 'docker compose up -d' in $DEPLOY_DIR so the app containers drop the old values"
fi
