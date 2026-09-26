#!/usr/bin/env bash
# Shared helpers for deploy.sh, rollback.sh and the backup scripts.
# Source it; do not run it. Expects DEPLOY_DIR to be set by the caller.

ALPINE_IMAGE="alpine:3.22"

log() { printf '%s %s: %s\n' "$(date -u +%FT%TZ)" "${LOG_TAG:-stonks}" "$*" >&2; }

# Read one KEY=value from deploy/.env without sourcing the whole file.
env_get() {
	local key="$1" default="${2:-}" line
	line="$(grep -E "^${key}=" "$DEPLOY_DIR/.env" 2>/dev/null | tail -n 1 || true)"
	if [ -n "$line" ]; then
		line="${line#*=}"
		line="${line%\"}"
		line="${line#\"}"
		printf '%s' "$line"
	else
		printf '%s' "$default"
	fi
}

# Write KEY=value into deploy/.env (replace or append).
env_set() {
	local key="$1" value="$2"
	if grep -qE "^${key}=" "$DEPLOY_DIR/.env"; then
		sed -i "s|^${key}=.*|${key}=${value}|" "$DEPLOY_DIR/.env"
	else
		printf '%s=%s\n' "$key" "$value" >>"$DEPLOY_DIR/.env"
	fi
}

data_path() { env_get STONKS_DATA_PATH /srv/stonks/data; }
snapshot_dir() { printf '%s/snapshots' "$(dirname "$(data_path)")"; }

compose() { docker compose --project-directory "$DEPLOY_DIR" "$@"; }

# Services that must be running: api, caddy, and scheduler when its profile is on.
expected_services() {
	printf 'api
caddy
'
	case ",$(env_get COMPOSE_PROFILES)," in
	*,scheduler,*) printf 'scheduler
' ;;
	esac
}

# Stop / start the two writers of /data (api, and scheduler when enabled).
stop_writers() {
	local svcs
	mapfile -t svcs < <(expected_services | grep -v caddy)
	compose stop "${svcs[@]}"
}
start_writers() {
	local svcs
	mapfile -t svcs < <(expected_services | grep -v caddy)
	compose start "${svcs[@]}"
}

# Prints the expected services that are not running (empty = all fine).
missing_services() {
	local running svc
	running="$(compose ps --status running --services 2>/dev/null || true)"
	for svc in $(expected_services); do
		grep -qx "$svc" <<<"$running" || printf '%s
' "$svc"
	done
}

# Ping a healthchecks.io-style URL: ping <url> [fail|start]. Empty URL = no-op.
ping_hc() {
	local url="$1" suffix="${2:-}"
	[ -n "$url" ] || return 0
	[ -n "$suffix" ] && url="$url/$suffix"
	curl -fsS -m 10 --retry 3 -o /dev/null "$url" || log "ping failed: ${url%%/*}//***"
}

# Post a one-line alert to the chat webhook (STONKS_NOTIFY_WEBHOOK_URL).
notify() {
	local level="$1" text="$2" url
	url="$(env_get STONKS_NOTIFY_WEBHOOK_URL)"
	log "$level: $text"
	[ -n "$url" ] || return 0
	local body
	body="$(printf '{"level":"%s","title":"stonks ops","message":"%s","text":"[%s] %s"}' \
		"$level" "$text" "$level" "$text")"
	curl -fsS -m 10 -H 'Content-Type: application/json' -d "$body" -o /dev/null "$url" ||
		log "webhook failed"
}

# Tar the whole data volume into snapshots/<name>.tar.gz (services must be stopped).
snapshot_create() {
	local name="$1" dir
	dir="$(snapshot_dir)"
	mkdir -p "$dir"
	docker run --rm -v "$(data_path):/data:ro" -v "$dir:/out" "$ALPINE_IMAGE" \
		tar czf "/out/$name.tar.gz" --exclude=./backups -C /data .
	log "snapshot $dir/$name.tar.gz"
}

# Replace the data volume's contents with a snapshot (services must be stopped).
snapshot_restore() {
	local file="$1" dir
	dir="$(snapshot_dir)"
	[ -f "$dir/$file" ] || {
		log "snapshot $dir/$file not found"
		return 1
	}
	docker run --rm -v "$(data_path):/data" -v "$dir:/in:ro" "$ALPINE_IMAGE" \
		sh -c "find /data -mindepth 1 -maxdepth 1 ! -name backups -exec rm -rf {} + && tar xzf /in/$file -C /data"
	log "restored $dir/$file"
}

# Keep the newest N pre-deploy snapshots.
snapshot_prune() {
	local keep="${1:-5}" dir
	dir="$(snapshot_dir)"
	[ -d "$dir" ] || return 0
	find "$dir" -maxdepth 1 -name 'pre-*.tar.gz' -printf '%T@ %p\n' |
		sort -rn | tail -n +"$((keep + 1))" | cut -d' ' -f2- | xargs -r rm -f
}

# Wait until the api container is healthy, the scheduler runs, and Caddy
# answers /api/health over HTTPS. Returns 1 on timeout.
wait_healthy() {
	local timeout="${1:-180}" waited=0 id status domain bind
	domain="$(env_get STONKS_DOMAIN localhost)"
	bind="$(env_get STONKS_BIND_IP 127.0.0.1)"
	[ "$bind" = "0.0.0.0" ] && bind=127.0.0.1
	while [ "$waited" -lt "$timeout" ]; do
		id="$(compose ps -q api || true)"
		status="$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo missing)"
		if [ "$status" = "healthy" ] &&
			[ -z "$(missing_services)" ] &&
			curl -fsS -m 5 -k --resolve "$domain:443:$bind" "https://$domain/api/health" >/dev/null; then
			log "healthy after ${waited}s"
			return 0
		fi
		sleep 5
		waited=$((waited + 5))
	done
	log "not healthy after ${timeout}s (api: $status)"
	compose ps >&2 || true
	compose logs --tail 50 api scheduler >&2 || true
	return 1
}
