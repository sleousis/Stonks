#!/usr/bin/env bash
# Deploy one image tag to this server (Phase 14.4).
#
#   pull -> stop -> snapshot (+ off-server backup) -> migrate -> start -> health
#                                   \-- any failure: restore snapshot, start previous tag
#
# Usage (on the VM, from anywhere):  deploy/scripts/deploy.sh v1.2.3
# The GitHub "Deploy" workflow runs exactly this over SSH.
set -Eeuo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC2034 # read by log() in lib.sh
LOG_TAG=deploy
# shellcheck source=lib.sh
. "$DEPLOY_DIR/scripts/lib.sh"
cd "$DEPLOY_DIR"

tag="${1:?usage: deploy.sh <tag, e.g. v1.2.3>}"
[ -f .env ] || {
	log ".env missing: cp .env.example .env and fill it in"
	exit 2
}
chmod 600 .env

previous="$(cat .deployed-tag 2>/dev/null || true)"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot="pre-${tag}-${ts}.tar.gz"
image="$(env_get STONKS_IMAGE)"
# The containers run as uid 10001; make sure it owns the data folder.
docker run --rm -v "$(dirname "$(data_path)"):/srv" "$ALPINE_IMAGE" \
	sh -c "mkdir -p /srv/$(basename "$(data_path)") && chown 10001:10001 /srv/$(basename "$(data_path)")"

log "deploying $image:$tag (previous: ${previous:-none})"
env_set STONKS_IMAGE_TAG "$tag"

rollback() {
	local reason="$1"
	trap - ERR
	notify error "deploy of $tag failed ($reason); rolling back to ${previous:-nothing}"
	stop_writers || true
	if [ -f "$(snapshot_dir)/$snapshot" ]; then
		snapshot_restore "$snapshot" || notify error "snapshot restore failed; data may be at $tag's schema"
	fi
	if [ -z "$previous" ]; then
		log "first deploy failed; nothing to roll back to"
		exit 1
	fi
	env_set STONKS_IMAGE_TAG "$previous"
	compose up -d --remove-orphans
	if wait_healthy 180; then
		notify warning "rolled back to $previous after failed deploy of $tag"
	else
		notify error "rollback to $previous is NOT healthy; see docs/runbooks/deploy-failed.md"
	fi
	exit 1
}
trap 'rollback "command failed at line $LINENO"' ERR

# 1. Pull first: a missing image fails before anything is stopped.
compose pull

# 2. Stop writers so the snapshot and migrations see a quiet lake.
stop_writers

# 3. Local snapshot for instant rollback, plus the off-server backup if set up.
snapshot_create "${snapshot%.tar.gz}"
if [ -n "$(env_get RESTIC_REPOSITORY)" ]; then
	"$DEPLOY_DIR/backup/backup.sh" --services-stopped --tag "pre-deploy-$tag" ||
		notify warning "pre-deploy off-server backup failed; local snapshot $snapshot kept"
fi

# 4. Migrations (both stores). Idempotent.
compose run --rm --no-deps api stonks db init

# 5. Start the new version and check it.
compose up -d --remove-orphans
if ! wait_healthy 180; then
	rollback "health check timed out"
fi
trap - ERR

printf '%s\n' "$previous" >.previous-tag
printf '%s\n' "$tag" >.deployed-tag
snapshot_prune 5

# Refresh the maintenance crontab (backups, restore test, host checks).
if command -v crontab >/dev/null 2>&1; then
	sed "s|@DEPLOY_DIR@|$DEPLOY_DIR|g" "$DEPLOY_DIR/crontab" | crontab -
fi

notify info "deployed $tag (previous: ${previous:-none})"
log "done"
