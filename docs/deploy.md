# Deploy

How to run Stonks on one small always-on machine for several traders: create the server, set secrets, deploy, update, roll back, back up, restore and monitor. Covers Phases 12.7 to 12.9, 14.1 to 14.10, 19.4 and 20.6 of the [roadmap](roadmap.md). Sizes and costs: [capacity.md](capacity.md).

## Pick where to run

Two choices, one stack. Both use the same image and the same `deploy/compose.yaml`.

| | Cloud VM | Home server |
|---|---|---|
| Machine | A rented VM (Hetzner, DigitalOcean and others) | A mini PC or your own PC |
| Cost | About 6 to 15 EUR a month | The hardware once, plus power |
| Uptime | The provider keeps it on | You keep it on (UPS, auto start) |
| Access | Tailscale, or public HTTPS with a domain | Tailscale only |
| AI model server | Needs a large, costly VM | Fits a PC with spare RAM or a GPU |
| Guide | [Cloud VM guide](#cloud-vm-guide) | [Local home server guide](#local-home-server-guide) |

Pick the cloud when you want no hardware to look after. Pick a home server when you have a spare machine, want the AI assistant on your own model, or want the data at home. Either way, backups go off the machine.

## The picture

```mermaid
flowchart LR
  subgraph tailnet["Tailscale tailnet (private)"]
    T1[Trader laptop]
    T2[Trader phone]
  end
  subgraph vm["Cloud VM (Ubuntu, Docker)"]
    C[Caddy :443<br/>HTTPS]
    A[api<br/>stonks serve<br/>REST + console]
    S[scheduler<br/>python -m stonks.scheduling]
    W[lab-worker, optional<br/>python -m stonks.lab.offload]
    V[("data volume /data<br/>lake, Parquet bars,<br/>state, artifacts")]
    C --> A
    A --- V
    S --- V
    W --- V
  end
  T1 --> C
  T2 --> C
  GH[GitHub Actions] -- SSH over tailnet --> vm
  GHCR[(GHCR image)] -. docker pull .-> vm
  V -. nightly restic, encrypted .-> B[(Backblaze B2 /<br/>Cloudflare R2)]
  HC[healthchecks.io] -. dead-man pings .- vm
```

- **One image** (`Dockerfile`) holds the API, the built console and the scheduler. It runs as a non-root user (uid 10001).
- **Compose** (`deploy/compose.yaml`) runs three services: `api`, `scheduler` and `caddy`, plus optional services behind profiles (see [Profiles](#profiles)). Everything the app writes lives on one volume, `/data`, which sits on an attached block volume on the host (`/srv/stonks/data`).
- **Private access** is the default: the server has no public port. Traders join the tailnet. Public HTTPS with Let's Encrypt is one setting away.

| File | Purpose |
|------|---------|
| `Dockerfile`, `.dockerignore` | Multi-stage build: Node builds the console, uv installs the package, slim runtime with a healthcheck on `/api/health/live`. Compose waits for `/api/health/ready` before Caddy and the scheduler start. |
| `deploy/compose.yaml` | `api` and `caddy`, plus profiles for `scheduler`, `lab-worker`, `restic` (backup), the IB Gateways and the model server. |
| `deploy/ibkr/` | The IB Gateway guide and the folder for its secret files (never committed). |
| `deploy/compose.tailscale.yaml` | Lets Caddy get its `*.ts.net` certificate from Tailscale. |
| `deploy/Caddyfile` | HTTPS, security headers, reverse proxy to the API. |
| `deploy/.env.example` | Every setting and secret the server needs (placeholders). |
| `deploy/cloud-init.yaml` | First-boot setup: users, Docker, firewall, automatic updates, data volume, Tailscale. |
| `infra/terraform/hetzner/` | Creates the server, firewall and volume on Hetzner Cloud. |
| `deploy/scripts/deploy.sh`, `rollback.sh` | Deploy with snapshot, migrations, health check and automatic rollback; manual rollback. |
| `deploy/backup/` | `backup.sh` (nightly), `restore-test.sh` (monthly), `restore.sh` (disaster). |
| `deploy/monitor/check-host.sh` | Disk, memory, load and container checks every 5 minutes. |
| `deploy/crontab` | The schedule for the three jobs above. Installed by every deploy. |
| `.github/workflows/release.yml`, `deploy.yml` | Build and publish on a tag; deploy over SSH. |

## Profiles

A plain `docker compose up -d` starts only the core. Profiles add the rest. Turn them on with `COMPOSE_PROFILES` in `deploy/.env` (comma separated) or with `--profile` on the command line.

| Profile | Adds | Memory | Notes |
|---------|------|--------|-------|
| none (core) | `api`, `caddy` | about 0.5 GB | Always on |
| `scheduler` | `scheduler` | about 0.2 GB | The daily loop and `broker_health`. On in the example `.env` |
| `lab-worker` | `lab-worker` | 3 GB limit (`STONKS_LAB_WORKER_MEMORY`) | [10. Lab offload](#10-lab-offload) |
| `backup` | `restic` | small, one-shot | `docker compose --profile backup run --rm restic ...` |
| `ibkr-paper` | `ib-gateway-paper` | 1 GB | [IB Gateway](#ib-gateway) |
| `ibkr-live` | `ib-gateway-live` | 1 GB | [IB Gateway](#ib-gateway) |
| `ai` | `model-server` (Ollama) | 8 GB limit (`STONKS_AI_MEMORY`) | [AI model server](#ai-model-server) |

`compose.tailscale.yaml` is not a profile. It is an overlay file that lets Caddy get its `*.ts.net` certificate from the host's Tailscale. Add it with `COMPOSE_FILE=compose.yaml:compose.tailscale.yaml`.

Example for a home server with paper trading and the assistant:

```dotenv
COMPOSE_PROFILES=scheduler,ibkr-paper,ai
```

## Cloud VM guide

The numbered sections below are the cloud path, start to finish:

1. Create the VM with Terraform (`infra/terraform/hetzner/`) or by hand with `deploy/cloud-init.yaml` (section 1).
2. Set the secrets in `deploy/.env` and in GitHub (section 2).
3. Deploy with a tag. GitHub Actions ships every release (sections 3 to 5).
4. Back up off the server, test restores and monitor (sections 6 to 8).
5. Pick the access mode: Tailscale (the default, no open port) or [public mode](#public-mode-no-tailscale) with a domain and Let's Encrypt.

A home server uses much of it too. Sections 2, 6, 7, 8 and 10 apply as written.

## 1. Create the server

Pick one. Both give the same result.

### With Terraform (Hetzner example)

You need: a Hetzner Cloud project and an API token (read/write), [Terraform](https://developer.hashicorp.com/terraform/install), two SSH key pairs (yours, and one for CI), and a Tailscale auth key ([deploy/tailscale/README.md](../deploy/tailscale/README.md), steps 1 to 4).

```bash
ssh-keygen -t ed25519 -f ~/.ssh/stonks_deploy -C github-actions-deploy -N ""   # CI key
cd infra/terraform/hetzner
cp terraform.tfvars.example terraform.tfvars      # fill in the two public keys
export TF_VAR_hcloud_token=...                     # never in a file
export TF_VAR_tailscale_auth_key=tskey-auth-...    # private mode
terraform init
terraform apply
```

The server boots, runs `cloud-init` (about 3 minutes) and appears in your tailnet as `stonks`. Rebuilding it from scratch is `terraform destroy && terraform apply` plus a deploy and a restore (section 7).

Terraform state contains the Tailscale key and server details. Keep it on your machine or in an encrypted remote backend. It is gitignored.

### By hand (any provider)

1. Create an Ubuntu 24.04 VM with 2 vCPU and 4 GB RAM, and attach a 20 GB block volume.
2. Open `deploy/cloud-init.yaml`, replace the five placeholders (see its header), and paste it as **user data**. Use the volume's device path (for example `/dev/disk/by-id/scsi-0DO_Volume_stonks`) for `volume_device`.
3. In the provider's firewall, allow **no inbound traffic** (Tailscale) or only SSH from your IP (public mode). Docker publishes ports past `ufw`, so the provider firewall is the real guard.

### Check the server

```bash
ssh admin@stonks          # over Tailscale (MagicDNS name)
cloud-init status         # "done"
docker version && ls -ld /srv/stonks/data /opt/stonks/deploy
```

## 2. Set the secrets

### On the server

```bash
# from your checkout of the repo
scp deploy/.env.example deploy@stonks:/opt/stonks/deploy/.env
ssh deploy@stonks 'chmod 600 /opt/stonks/deploy/.env'
ssh -t deploy@stonks 'nano /opt/stonks/deploy/.env'
```

Fill in at least `STONKS_IMAGE` (`ghcr.io/<owner>/<repo>`, lower case), `STONKS_DOMAIN`, `STONKS_API_TOKEN` (`openssl rand -hex 32`), `EODHD_API_KEY`, the `RESTIC_*` and `AWS_*` backup values, and the `HC_PING_*` URLs.

Secrets live **only** in this file. They are never baked into the image and never committed.

### In GitHub (Settings > Secrets and variables > Actions)

| Name | Kind | Value |
|------|------|-------|
| `DEPLOY_HOST` | secret | `stonks` (Tailscale name) or the server's IP |
| `DEPLOY_USER` | secret | `deploy` (optional, this is the default) |
| `DEPLOY_SSH_KEY` | secret | Private key `~/.ssh/stonks_deploy` |
| `DEPLOY_KNOWN_HOSTS` | secret | Output of `ssh-keyscan stonks` run from a tailnet machine |
| `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET` | secret | Tailscale OAuth client with tag `tag:ci` (skip in public mode) |
| `STONKS_URL` | variable | `https://stonks.<tailnet>.ts.net` or your domain (optional; enables the post-deploy version check) |

Also create an environment named `production` (Settings > Environments). Add yourself as a required reviewer if every deploy should wait for a click.

Turn on **secret scanning** and **push protection** (Settings > Code security). They cannot be set from a file.

### Rotation

| Secret | When | How |
|--------|------|-----|
| `STONKS_API_TOKEN` | Every 90 days, and when a trader leaves | New value in `.env`, `docker compose up -d`, tell traders |
| Broker and data keys | Every 90 days or per the vendor | Vendor console, then `.env` |
| `DEPLOY_SSH_KEY` | Yearly | New key pair, update `~deploy/.ssh/authorized_keys` and the secret |
| B2/R2 keys | Yearly | New app key scoped to the bucket, then `.env`. Keep `RESTIC_PASSWORD` (it encrypts the old snapshots) |
| Tailscale OAuth client | Yearly | New client, update both secrets |

Optional: keep an encrypted copy of `.env` in the repo with [sops](https://github.com/getsops/sops) and an age key that only you hold.

## 3. First deploy

```mermaid
sequenceDiagram
  participant You
  participant GH as GitHub Actions
  participant VM as Server
  You->>GH: git tag v0.1.0 && git push --tags
  GH->>GH: Release: build amd64+arm64 image, SBOM, provenance
  GH->>GH: GitHub release with changelog
  GH->>VM: Deploy: rsync deploy/, docker login (job token)
  GH->>VM: deploy.sh v0.1.0
  VM->>VM: pull, stop, snapshot, backup, db init, start
  VM-->>GH: healthy (or rolled back)
```

1. Bump `version` in `pyproject.toml`, refresh the changelog (`git cliff --tag v0.1.0 -o CHANGELOG.md`), commit.
2. `git tag v0.1.0 && git push origin v0.1.0`.
3. Watch **Release**, then **Deploy**, in the Actions tab.
4. On the server, run `docker compose run --rm api stonks users bootstrap --email you@example.com` once. Then open `https://stonks.<tailnet>.ts.net`, sign in and set up the second factor. Every call needs a sign-in or a token. Behind Caddy the API trusts forwarded client IPs only from the Compose network (`STONKS_DOCKER_SUBNET`).

The release fails on purpose if the tag does not match `pyproject.toml`.

## 4. Updates

Every release deploys itself. What `deploy.sh` does, in order:

```mermaid
flowchart TD
  P[Pull new image] --> St[Stop api + scheduler]
  St --> Sn[Local snapshot of /data]
  Sn --> Bk[Off-server restic backup]
  Bk --> M[stonks db init<br/>migrations]
  M --> Up[Start new tag]
  Up --> H{Healthy within 3 min?}
  H -- yes --> Done[Record tag, prune old snapshots,<br/>install crontab, alert 'deployed']
  H -- no --> R[Restore snapshot, start previous tag,<br/>alert 'rolled back']
  M -- fails --> R
```

- Downtime is the length of the snapshot plus migrations, usually under a minute. Deploy outside the daily loop (22:30 to 23:00 UTC).
- Tags with a suffix (`v1.3.0-rc.1`) are published as pre-releases and are not deployed automatically. Deploy them with **Actions > Deploy > Run workflow**.
- Dependabot opens grouped update PRs every Monday (Python, npm, Docker, Compose, Actions, Terraform). CI must pass before merge.
- The OS patches itself (`unattended-upgrades`) and reboots at 04:30 UTC when needed. The containers restart on their own.
- The monthly patch window (the 8th, 05:00 UTC, `deploy/crontab`) refreshes the Caddy and restic images and removes images unused for 90 days.

## 5. Roll back

Automatic: a failed deploy rolls itself back (section 4).

By hand, from GitHub: **Actions > Deploy > Run workflow** with the older tag. It runs the full deploy (snapshot, migrations are no-ops for older code, health check).

By hand, on the server:

```bash
/opt/stonks/deploy/scripts/rollback.sh               # previous tag, data as is
/opt/stonks/deploy/scripts/rollback.sh --with-data   # also restore the pre-deploy snapshot
/opt/stonks/deploy/scripts/rollback.sh v1.2.0        # a specific tag
```

Use `--with-data` only if the newer version's migrations broke the older one. It discards every tick, order and ingest since that deploy. See [runbooks/deploy-failed.md](runbooks/deploy-failed.md).

## 6. Backups

| What | Where | When | Keeps |
|------|-------|------|-------|
| Pre-deploy snapshot | `/srv/stonks/snapshots/pre-<tag>-<time>.tar.gz` | Every deploy | Last 5 |
| Off-server backup (restic, encrypted) | B2 or R2 bucket | 02:30 UTC nightly, and every deploy | 7 daily, 4 weekly, 12 monthly |
| Restore test | Scratch volume, wiped after | 1st of the month, 04:00 UTC | Result in healthchecks.io. Fails if the state DB is missing or lost rows |

`backup.sh` uses `python -m stonks.ops backup` when the image has it (a consistent copy with no downtime, written to `/data/backups`). Otherwise it stops the api and scheduler for the upload and backs up the whole volume.

Set up the bucket:

- **Backblaze B2**: create a private bucket and an application key limited to it. `RESTIC_REPOSITORY=s3:https://s3.<region>.backblazeb2.com/<bucket>/stonks`.
- **Cloudflare R2**: create a bucket and an R2 API token with object read/write on it. `RESTIC_REPOSITORY=s3:https://<account-id>.r2.cloudflarestorage.com/<bucket>/stonks` and `AWS_DEFAULT_REGION=auto`.

Store `RESTIC_PASSWORD` in your password manager too. Without it the backups cannot be read.

Useful commands (in `/opt/stonks/deploy`):

```bash
./backup/backup.sh                                             # back up now
docker compose --profile backup run --rm restic snapshots      # list backups
./backup/restore-test.sh                                       # prove a restore works
```

## 7. Restore

After losing the server: create a new one (section 1), set `.env` (same `RESTIC_*` values), deploy the same tag, then:

```bash
/opt/stonks/deploy/backup/restore.sh            # latest; or pass a snapshot id
```

It asks you to type `RESTORE` and keeps a local copy of the current data. Then `python -m stonks.ops restore-snapshot` brings back the state DB, the artifacts and the lake together. It moves the files already in `/data` aside as `<name>.pre-restore-<time>`, never deletes them, and checks that the users, strategies and orders row counts match the snapshot. A snapshot without the state DB or the lake is refused and nothing changes. Then it restarts and checks health.

The monthly restore test runs the same restore into a scratch folder and then `python -m stonks.ops check-restore`. It fails when the state DB is missing, empty or has fewer rows than the snapshot.

## 8. Monitoring

```mermaid
flowchart LR
  U[Uptime check<br/>every 1-5 min] -->|GET /api/health| C[Caddy]
  J[cron: backup, restore test,<br/>host check] -->|ping / fail| HC[healthchecks.io]
  SCH[scheduler: ingest, tick, health] -.->|ping / fail| HC
  HC -->|missed or failed| W[Webhook / email]
  J -->|problem| W
  API[stonks alerts] -->|STONKS_NOTIFY_WEBHOOK_URL| W
```

1. **Dead-man checks.** In [healthchecks.io](https://healthchecks.io) (free for 20 checks) or a self-hosted Uptime Kuma, create one check per job and put its ping URL in `.env`:

   | Variable | Schedule | Grace |
   |----------|----------|-------|
   | `HC_PING_HOST` | every 5 minutes | 15 minutes |
   | `HC_PING_BACKUP` | daily 02:30 UTC | 2 hours |
   | `HC_PING_RESTORE_TEST` | monthly, 1st, 04:00 UTC | 1 day |
   | `HC_PING_INGEST`, `HC_PING_TICK`, `HC_PING_HEALTH` | weekdays after the close | 1 hour |

   The first three are pinged by the scripts in `deploy/`. The last three are for the scheduler (see "Needs app support" below). Connect healthchecks.io to the same chat channel as `STONKS_NOTIFY_WEBHOOK_URL`.

2. **Uptime check.** Public mode: point an external monitor (healthchecks.io does not do this; use UptimeRobot, Better Stack or Uptime Kuma) at `https://<domain>/api/health`. Tailscale mode: nothing outside can reach the server, so `HC_PING_HOST` doubles as the uptime signal. If the server dies, the pings stop and you get an alert.

3. **Host limits.** `check-host.sh` alerts when disk passes 80 %, memory 90 %, or the 15-minute load 90 % of the cores, and when a container is down or the API unhealthy. Change the limits with `ALERT_*_PCT` in `.env`. Repeated alerts are sent at most once an hour.

4. **Logs.** Docker keeps 5 x 10 MB of JSON logs per container. Read them with `docker compose logs -f api scheduler`. Stonks logs are structlog JSON, so `| jq` works.

## 9. Size and cost

| Item | Choice | About per month |
|------|--------|-----------------|
| VM | 2 vCPU, 4 GB RAM, 40 GB disk (Hetzner CX22 class) | 4 to 6 EUR |
| Block volume | 20 GB | 1 EUR |
| Object storage | B2 (first 10 GB free, then about 6 USD/TB) or R2 (10 GB free) | 0 to 1 EUR |
| Tailscale | Personal or Starter plan | 0 to 6 USD per user |
| healthchecks.io | Free plan | 0 |
| **Total** | | **about 6 to 10 EUR** plus Tailscale seats |

Prices change; check the provider pages. DigitalOcean's 2 vCPU / 4 GB droplet costs about 24 USD.

Sizing:

Measured sizes for 1, 5 and 20 traders, and how they were measured, are in [capacity.md](capacity.md). In short:

- **RAM.** The API idles at a few hundred MB. Each lab or backtest job adds more (`[api].max_concurrent_jobs`, default 2). 4 GB is enough for a few traders. Move to 8 GB if the monitor reports memory alerts during jobs.
- **Disk.** Daily bars cost about 12 KB per ticker-year in Parquet and 23 KB in the DuckDB table. Intraday bars grow much faster. Keep the volume under 70 % full, since snapshots need room too. Grow it with `volume_size_gb` (or in the provider console), then `sudo resize2fs /dev/disk/by-label/stonks-data`.
- **CPU.** Run heavy lab work on the lab worker (next section) so it never slows the console.

## 10. Lab offload

Tuning, survival suites, sweeps and MCPT can keep every core busy for minutes. Inside the API they also compete with traders' requests. The lab worker runs them in their own container with their own limits.

```mermaid
flowchart LR
  T[Trader] -->|POST /api/lab/runs| A[api]
  A -->|1. publish if stale| SN[("lab_snapshots/<br/>read-only lake copy")]
  A -->|2. jobs row, executor=worker| Q[("state.sqlite<br/>jobs queue")]
  W[lab-worker] -->|3. claim| Q
  W -->|4. read| SN
  W -->|5. result, heartbeat| Q
  A -->|6. result, events| T
```

1. The API gets a lab run, sweep or Studio lab run. When `STONKS_LAB_EXECUTOR=worker`, it does not run it. It writes the job row with `executor = worker`.
2. First it makes sure a fresh read-only copy of the lake exists under `/data/lab_snapshots`. It builds a new one when an ingest or bar fetch finished since, a universe, its members or an index history changed, or the copy is older than an hour. With Parquet bars the copy is small: the bar files are hard links.
3. The worker claims the oldest queued job, opens the copy read-only and runs the same handler the API would. DuckDB allows one writer, and the worker never writes the lake.
4. Progress, the result and registered strategies go to the real state DB and artifacts. The job row, its event stream and its result route work as before.
5. Cancel works: the worker checks a flag at every heartbeat and stops at the next checkpoint. A worker that dies stops its heartbeat, and its job fails after two minutes. A worker you stop (SIGTERM, `docker compose restart`) puts its running job back in the queue, and a restart with the same `--id` reuses its row.

Turn it on:

```bash
# deploy/.env
COMPOSE_PROFILES=scheduler,lab-worker
STONKS_LAB_EXECUTOR=worker
STONKS_LAB_WORKER_CPUS=2      # CPU limit, also the size of its process pool
STONKS_LAB_WORKER_MEMORY=3g   # memory limit
docker compose up -d
```

- The worker gets a quarter of the API's CPU weight, so the API wins when both are busy.
- A lab run that must fetch missing data first (`ensure_data`) stays in the API, because the fetch writes the lake. Run `stonks universe ensure` first to offload it.
- Run more workers to run more jobs at once: `docker compose up -d --scale lab-worker=2`. Each runs one job at a time.
- For a big search, resize the VM for an hour (Hetzner and DigitalOcean resize in about a minute), raise `STONKS_LAB_WORKER_CPUS`, and resize back.
- Watch it: `docker compose exec lab-worker python -m stonks.lab.offload status`, the `lab_queue` health check and the `stonks_lab_*` metrics ([operations.md](operations.md#lab-worker)).

The worker must share the data folder with the API on a local disk: the queue is the SQLite state DB, and SQLite on a network file system is not safe. A worker on another machine (the 32-core PC) needs a queue over the API. That is not built yet.

## Local home server guide

The same stack on a machine at home. Traders reach it over Tailscale. Your router opens no port.

```mermaid
flowchart LR
  P[Phone or laptop<br/>Stonks PWA] --> TS[Tailscale tailnet<br/>WireGuard]
  subgraph home["Home server (Docker)"]
    C[Caddy :443] --> A[api]
    S[scheduler] --> A
    A --> G[ib-gateway<br/>ibkr network]
    A --> M[model-server<br/>optional]
    V[("/data")]
    A --- V
  end
  TS --> C
  G --> IB[(IBKR servers)]
  V -. restic, encrypted .-> B[(Cloud bucket or<br/>another machine)]
  UPS[UPS] -. NUT upsmon .-> home
```

### Hardware

- **A low-cost mini PC** is plenty: an Intel N100 class box with 16 GB RAM and an SSD (256 GB or more). It idles at a few watts.
- **Or your own PC**, if it stays on.
- **Memory.** The core needs about 1 GB. Add 1 GB per IB Gateway and 3 GB for a lab worker. 16 GB leaves room for all of them.
- **AI model server.** A 7 to 8B model at 4 bit needs about 6 GB more RAM and runs slowly on a mini PC's CPU. For a quick assistant use a PC with 32 GB RAM or an NVIDIA GPU with 8 GB of VRAM or more.
- **An SSD**, not an SD card or a USB stick. SQLite and DuckDB write often.

### Install Docker

- **Linux (Ubuntu or Debian, recommended).** Install Docker Engine and the Compose plugin from [docs.docker.com](https://docs.docker.com/engine/install/). Add your user to the `docker` group.
- **Windows.** Install Docker Desktop with the WSL 2 backend. Keep the repo and the data folder inside the WSL file system (for example `/home/you/stonks`), not on `C:`. SQLite over the Windows mount is slow and not safe.

### Clone and configure

```bash
git clone https://github.com/OWNER/stonks.git /opt/stonks
cd /opt/stonks/deploy
cp .env.example .env && chmod 600 .env
sudo mkdir -p /srv/stonks/data        # or set STONKS_DATA_PATH to your folder
sudo chown 10001:10001 /srv/stonks/data
```

Fill in `.env` as in [2. Set the secrets](#2-set-the-secrets). On a home server:

- `STONKS_IMAGE` and `STONKS_IMAGE_TAG`: the published image and a release tag.
- `STONKS_DATA_PATH`: a folder on the SSD.
- `COMPOSE_FILE=compose.yaml:compose.tailscale.yaml` and `STONKS_DOMAIN=<machine>.<tailnet>.ts.net`.

Then start it and create the first user:

```bash
docker compose up -d
docker compose run --rm api stonks users bootstrap --email you@example.com
```

To update, run `./scripts/deploy.sh <tag>`. It takes the same snapshot, runs the migrations and checks health, as on the cloud. Install `deploy/crontab` for the nightly backup, the restore test and the host check.

### Phone access with Tailscale

1. Install Tailscale on the server (`curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up`).
2. In the Tailscale admin console turn on **MagicDNS** and **HTTPS certificates** ([deploy/tailscale/README.md](../deploy/tailscale/README.md)).
3. Keep `compose.tailscale.yaml` in `COMPOSE_FILE`. Caddy then gets a real `*.ts.net` certificate from Tailscale.
4. Optional: set `STONKS_BIND_IP` to the output of `tailscale ip -4`, so Caddy listens on the tailnet only and not on your home network.
5. Install Tailscale on each phone, open `https://<machine>.<tailnet>.ts.net` and add the console to the home screen. The PWA and web push work over the tailnet.

`tailscale serve` can also put Tailscale's own HTTPS in front of Caddy. The overlay above is simpler and matches the cloud setup.

### Start on boot

- Every service has `restart: unless-stopped`, so the containers come back when Docker starts.
- **Linux:** `sudo systemctl enable docker containerd`.
- **BIOS:** set "Restore on AC power loss" to **Power on**. The machine then boots by itself after a power cut.
- **Windows:** in Docker Desktop turn on "Start Docker Desktop when you sign in", and let Windows sign in automatically. Docker Desktop runs only after a sign-in. A Linux box avoids this.

### Sleep off

Turn off sleep, hibernate and disk spin-down. A sleeping server misses the tick, and the gateway loses its session.

- Linux: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`
- Windows: in Power options set sleep to "Never", then run `powercfg /hibernate off`.

### UPS and power loss

- A small UPS (600 VA or more) rides through short cuts and gives time for a clean shutdown on long ones.
- Connect it by USB and run NUT (`sudo apt install nut`). Set `upsmon` to shut the machine down at low battery. The shutdown stops Docker, which stops each container within its grace period.
- SQLite (WAL mode) and DuckDB survive a clean stop. A hard power cut is usually fine too, but a torn write is possible. That is what the backups are for.

When the power comes back:

- The machine boots (the BIOS setting), Docker starts and the containers restart.
- The **scheduler** catches up the latest missed run of each job (tick, ingest, health) within 72 hours and skips older ones.
- The **IB Gateway** logs in again by itself. It needs your IBKR Mobile approval only if the weekly session expired in the meantime.
- **Auto orders are never sent late.** Tickets expire at the submit deadline, and the next tick decides again from fresh data.

### Backups off the machine

A backup on the same disk is not a backup. A dead SSD, a theft or a fire takes both copies.

- Use the `backup` profile (restic, encrypted) with a cloud bucket, as in [6. Backups](#6-backups). B2 or R2 cost cents a month at this size.
- Or back up to another machine, for example `RESTIC_REPOSITORY=sftp:user@nas:/backups/stonks` or a restic REST server.
- Test a restore now and then: `./backup/restore-test.sh`. The crontab runs it monthly.
- Keep `RESTIC_PASSWORD` in your password manager, not only on the server.

### Home network

- Open **no ports** on your router. No port forwarding, no UPnP.
- Tailscale connects out only, so it works behind any home router.
- The gateway also connects out to IBKR only.

## IB Gateway

Live and broker paper trading at Interactive Brokers go through IB Gateway containers. The full guide is [deploy/ibkr/README.md](../deploy/ibkr/README.md). It covers the secret files, the dedicated username, the weekly 2FA on IBKR Mobile, the daily restart and a one-off VNC login.

```bash
# after creating deploy/ibkr/secrets/paper_username.txt and paper_password.txt
docker compose --profile ibkr-paper up -d
# later, after creating live_username.txt and live_password.txt
docker compose --profile ibkr-live up -d
```

- The gateways sit on the internal `ibkr` network. Only `api` and `scheduler` can reach them. Nothing is published on the host.
- The credentials come from Docker secret files that only the gateway reads.
- **Never run the paper and live gateways with the same username.** Each login kicks the other off.
- Each gateway needs about 1 GB of memory.

## AI model server

The optional `ai` profile runs an [Ollama](https://ollama.com) model server for the AI assistant (roadmap 20.4). It serves an OpenAI-compatible API on the default network only, with no port on the host.

```bash
# in deploy/.env
COMPOSE_PROFILES=scheduler,ai
STONKS_AI_BASE_URL=http://model-server:11434/v1
```

```bash
docker compose up -d
docker compose exec model-server ollama pull <model>
```

- Models live in the `models` volume, several GB each.
- `STONKS_AI_MEMORY` sets the memory limit (default 8 GB).
- With an NVIDIA GPU and the NVIDIA container toolkit, add `gpus: all` to the service.
- Any other OpenAI-compatible endpoint (vLLM, a llama.cpp server) works too. Point `STONKS_AI_BASE_URL` at it.

## Public mode (no Tailscale)

1. Point a DNS `A` (and `AAAA`) record at the server.
2. Terraform: `public_https = true` and `ssh_allowed_cidrs = ["<your ip>/32"]`, no Tailscale key.
3. `.env`: delete the `COMPOSE_FILE` line, set `STONKS_DOMAIN=stonks.example.com`.
4. GitHub: `DEPLOY_HOST` is the public IP; leave the Tailscale secrets empty. Allowing GitHub-hosted runners to SSH in means opening port 22 widely; prefer Tailscale or a self-hosted runner.

Caddy then gets a Let's Encrypt certificate by itself. Everyone on the internet can reach the login surface, so keep `STONKS_API_TOKEN` long and rotated.

## Windows without Docker

For a single-user setup on Windows, run `uv run stonks serve` as a service with [WinSW](https://github.com/winsw/winsw) or NSSM (working directory: the repo; environment: `.env`), and schedule the daily loop with Task Scheduler as in [operations.md](operations.md). Backups then use `stonks backup` (Phase 12.4) and any file-sync tool.

## Needs app support

These parts depend on code outside `deploy/`:

- `python -m stonks.scheduling` (Phase 12.2) is the scheduler service's command. Until the image has it, leave `COMPOSE_PROFILES` empty in `.env` (the service then does not start and health checks do not expect it) and run the daily loop with the cron lines from [operations.md](operations.md), using `docker compose exec api stonks ...`.
- Dead-man pings from the scheduler (`HC_PING_INGEST`, `HC_PING_TICK`, `HC_PING_HEALTH`).
- `python -m stonks.ops backup` (Phase 12.4) for backups without downtime.
- `[api]` host and allowed hosts cannot be set from the environment yet, so Caddy presents requests to the API as `localhost` (see `deploy/Caddyfile`).
