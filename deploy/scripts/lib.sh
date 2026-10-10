#!/usr/bin/env bash
# Shared helpers for deploy.sh, rollback.sh and the backup scripts.
# Source it; do not run it. Expects DEPLOY_DIR to be set by the caller.

ALPINE_IMAGE="alpine:3.22"

log() { printf '%s %s: %s\n' "$(date -u +%FT%TZ)" "${LOG_TAG:-stonks}" "$*" >&2; }

# Keys that only the backup tool and the host scripts read. They live in
# deploy/backup.env, which only the restic container gets, never in
# deploy/.env, which every app container gets (docs/deploy.md).
BACKUP_ENV_KEYS="RESTIC_REPOSITORY RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION B2_ACCOUNT_ID B2_ACCOUNT_KEY HC_PING_BACKUP HC_PING_RESTORE_TEST HC_PING_HOST"

# Turn the raw text after KEY= into its value, the way Compose reads an
# env_file: 'single quotes' keep the text as it is, "double quotes" take
# backslash escapes, and an unquoted value ends at a " #" comment.
env_unquote() {
	local raw="$1" out="" ch i=0
	raw="${raw#"${raw%%[![:space:]]*}"}"
	case "$raw" in
	\'*)
		raw="${raw#\'}"
		printf '%s' "${raw%%\'*}"
		;;
	\"*)
		raw="${raw#\"}"
		while [ "$i" -lt "${#raw}" ]; do
			ch="${raw:i:1}"
			if [ "$ch" = '"' ]; then
				break
			elif [ "$ch" = "\\" ] &&[ $((i + 1)) -lt "${#raw}" ]; then
				i=$((i + 1))
				ch="${raw:i:1}"
				case "$ch" in
				n) ch=$'\n' ;;
				r) ch=$'\r' ;;
				esac
			fi
			out+="$ch"
			i=$((i + 1))
		done
		printf '%s' "$out"
		;;
	*)
		sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//' <<<"$raw" | tr -d '\n'
		;;
	esac
}

# Read one KEY=value from a file without sourcing it: file_get FILE KEY [default].
file_get() {
	local file="$1" key="$2" default="${3:-}" line
	line="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 || true)"
	if [ -n "$line" ]; then
		env_unquote "${line#*=}"
	else
		printf '%s' "$default"
	fi
}

# Read one KEY=value from deploy/.env.
env_get() { file_get "$DEPLOY_DIR/.env" "$@"; }

# True when a backup value is empty or still a placeholder from
# backup.env.example, so it does not count as set. The old "change-me"
# password is a real (weak) password that encrypts old snapshots, so it counts.
backup_placeholder() {
	case "$1" in
	"" | *your-bucket*) return 0 ;;
	esac
	return 1
}

# True when deploy/backup.env holds a real value for KEY. A line copied
# from the example, empty or a placeholder, does not count.
backup_env_has() {
	! backup_placeholder "$(file_get "$DEPLOY_DIR/backup.env" "$1")"
}

# Read a backup key: deploy/backup.env first, then deploy/.env (the old
# layout, before migrate-backup-env.sh).
backup_env_get() {
	local key="$1" default="${2:-}" value
	if backup_env_has "$key"; then
		file_get "$DEPLOY_DIR/backup.env" "$key"
		return 0
	fi
	value="$(env_get "$key")"
	if backup_placeholder "$value"; then
		printf '%s' "$default"
	else
		printf '%s' "$value"
	fi
}

# Backup keys that still have a value in deploy/.env (the old layout).
legacy_backup_keys() {
	local key
	for key in $BACKUP_ENV_KEYS; do
		[ -n "$(env_get "$key")" ] && printf '%s\n' "$key"
	done
	return 0
}

# Warn while deploy/.env still holds backup secrets: every app container
# can read them there. warn_legacy_backup_env [log|notify].
warn_legacy_backup_env() {
	local how="${1:-log}" keys text
	keys="$(legacy_backup_keys | tr '\n' ' ')"
	[ -n "$keys" ] || return 0
	text="deploy/.env still holds backup secrets (${keys% }), so the app containers can read them. Move them to deploy/backup.env with deploy/scripts/migrate-backup-env.sh, then run docker compose up -d"
	if [ "$how" = notify ]; then
		notify warning "$text"
	else
		log "warning: $text"
	fi
}

# Run the restic container. On the old layout, the backup keys still in
# deploy/.env (and not set in backup.env) go to the container by name from
# this shell's environment, so they never show on a command line.
restic_run() {
	(
		args=()
		for key in $(legacy_backup_keys); do
			backup_env_has "$key" && continue
			export "$key=$(env_get "$key")"
			args+=(-e "$key")
		done
		compose --profile backup run --rm -T "${args[@]}" restic "$@"
	)
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

# Services that must be running: api, caddy, and scheduler and lab-worker
# when their profiles are on.
expected_services() {
	local profiles
	profiles=",$(env_get COMPOSE_PROFILES),"
	printf 'api
caddy
'
	case "$profiles" in
	*,scheduler,*) printf 'scheduler
' ;;
	esac
	case "$profiles" in
	*,lab-worker,*) printf 'lab-worker
' ;;
	esac
}

# Stop / start the writers of /data: api, and scheduler and lab-worker when
# enabled (the lab worker writes job state to state.sqlite and snapshots to
# /data/lab_tmp, so a snapshot or restore must not run under it).
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
