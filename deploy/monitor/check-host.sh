#!/usr/bin/env bash
# Host checks every 5 minutes (Phase 14.6, 14.10): disk, memory, load and
# container health. Alerts go to STONKS_NOTIFY_WEBHOOK_URL; a clean run pings
# HC_PING_HOST so a dead server (or a dead cron) is noticed too.
#
# Usage: deploy/monitor/check-host.sh
set -Eeuo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC2034 # read by log() in lib.sh
LOG_TAG=check-host
# shellcheck source=../scripts/lib.sh
. "$DEPLOY_DIR/scripts/lib.sh"
cd "$DEPLOY_DIR"

disk_limit="$(env_get ALERT_DISK_PCT 80)"
mem_limit="$(env_get ALERT_MEM_PCT 90)"
load_limit="$(env_get ALERT_LOAD_PCT 90)"
problems=()

# Disk: the root disk and the data volume.
for path in / "$(data_path)"; do
	[ -e "$path" ] || continue
	used="$(df --output=pcent "$path" | tail -n 1 | tr -dc '0-9')"
	if [ "$used" -ge "$disk_limit" ]; then
		problems+=("disk $path ${used}% used (limit ${disk_limit}%)")
	fi
done

# Memory: share of RAM not available.
mem_used="$(awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {printf "%d", (t-a)*100/t}' /proc/meminfo)"
if [ "$mem_used" -ge "$mem_limit" ]; then
	problems+=("memory ${mem_used}% used (limit ${mem_limit}%)")
fi

# CPU: 15-minute load as a share of the core count.
load_pct="$(awk -v cores="$(nproc)" '{printf "%d", $3*100/cores}' /proc/loadavg)"
if [ "$load_pct" -ge "$load_limit" ]; then
	problems+=("load ${load_pct}% of $(nproc) cores over 15 min (limit ${load_limit}%)")
fi

# Containers: every service that should run is running, api is healthy.
for svc in $(missing_services); do
	problems+=("container $svc is not running")
done
api_id="$(compose ps -q api 2>/dev/null || true)"
if [ -n "$api_id" ]; then
	health="$(docker inspect -f '{{.State.Health.Status}}' "$api_id" 2>/dev/null || echo unknown)"
	[ "$health" = "healthy" ] || problems+=("api container is $health")
fi

hc="$(env_get HC_PING_HOST)"
if [ "${#problems[@]}" -eq 0 ]; then
	ping_hc "$hc"
	exit 0
fi

# Alert at most once an hour for the same set of problems.
summary="$(printf '%s; ' "${problems[@]}")"
state="${XDG_STATE_HOME:-$HOME/.local/state}/stonks-check-host"
mkdir -p "$(dirname "$state")"
now="$(date +%s)"
last_summary="$(sed -n 1p "$state" 2>/dev/null || true)"
last_time="$(sed -n 2p "$state" 2>/dev/null || echo 0)"
if [ "$summary" != "$last_summary" ] || [ $((now - last_time)) -ge 3600 ]; then
	notify warning "host check: $summary"
	printf '%s\n%s\n' "$summary" "$now" >"$state"
fi
ping_hc "$hc" fail
exit 1
