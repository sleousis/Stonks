#!/usr/bin/env bash
# One-command rollback to the previously deployed tag (Phase 14.4).
#
# Usage:
#   deploy/scripts/rollback.sh               # previous tag, keep data as is
#   deploy/scripts/rollback.sh --with-data   # also restore the snapshot taken
#                                            # before the current tag was deployed
#   deploy/scripts/rollback.sh v1.2.2        # a specific tag
#
# --with-data throws away everything written since that deploy (ticks, orders,
# ingests). Use it only when the newer version's migrations broke the old one.
set -Eeuo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC2034 # read by log() in lib.sh
LOG_TAG=rollback
# shellcheck source=lib.sh
. "$DEPLOY_DIR/scripts/lib.sh"
cd "$DEPLOY_DIR"

with_data=0
target=""
for arg in "$@"; do
	case "$arg" in
	--with-data) with_data=1 ;;
	-h | --help)
		sed -n '2,12p' "$0"
		exit 0
		;;
	*) target="$arg" ;;
	esac
done

current="$(cat .deployed-tag 2>/dev/null || true)"
[ -n "$target" ] || target="$(cat .previous-tag 2>/dev/null || true)"
[ -n "$target" ] || {
	log "no previous tag recorded; pass one: rollback.sh v1.2.3"
	exit 2
}

log "rolling back ${current:-unknown} -> $target"
stop_writers

if [ "$with_data" = 1 ]; then
	snap="$(find "$(snapshot_dir)" -maxdepth 1 -name "pre-${current}-*.tar.gz" -printf '%f\n' 2>/dev/null | sort | tail -n 1)"
	[ -n "$snap" ] || {
		log "no pre-deploy snapshot for $current"
		exit 1
	}
	# Keep what we are about to overwrite, just in case.
	snapshot_create "before-rollback-$(date -u +%Y%m%dT%H%M%SZ)"
	snapshot_restore "$snap"
fi

env_set STONKS_IMAGE_TAG "$target"
compose up -d --remove-orphans
if wait_healthy 180; then
	printf '%s\n' "$current" >.previous-tag
	printf '%s\n' "$target" >.deployed-tag
	notify warning "manual rollback ${current:-unknown} -> $target done"
else
	notify error "rollback to $target is NOT healthy; see docs/runbooks/deploy-failed.md"
	exit 1
fi
