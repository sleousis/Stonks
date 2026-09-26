# Private access with Tailscale

Traders reach the console over a private network (a "tailnet"). The server opens no public port at all.

```text
trader laptop ──┐
trader phone  ──┼── tailnet (WireGuard) ──> stonks VM ──> Caddy :443 ──> api :8000
GitHub Actions ─┘   (tag:ci, deploy only)      no public ports
```

## One-time setup

1. Create a Tailscale account and tailnet (the free plan covers a few users).
2. Admin console, **DNS**: turn on **MagicDNS** and **HTTPS certificates**.
3. Admin console, **Access controls**: paste [`acl.hujson`](acl.hujson) and replace the emails.
4. Admin console, **Settings > Keys**: create an auth key that is **pre-approved**, **not reusable**, and tagged `tag:stonks`. It goes into Terraform as `TF_VAR_tailscale_auth_key` (or into `cloud-init.yaml` by hand). It is only used once, at first boot.
5. Admin console, **Settings > OAuth clients**: create a client with the `auth_keys` write scope and tag `tag:ci`. Save its id and secret as GitHub secrets `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET`. The deploy workflow uses it to join the tailnet for the length of one job.
6. Invite each trader to the tailnet. They install the Tailscale app and open `https://stonks.<tailnet>.ts.net`.

## Server settings

In `deploy/.env`:

```dotenv
COMPOSE_FILE=compose.yaml:compose.tailscale.yaml
STONKS_DOMAIN=stonks.<tailnet>.ts.net
# Optional: bind Caddy to the Tailscale address only (tailscale ip -4)
STONKS_BIND_IP=100.x.y.z
```

`compose.tailscale.yaml` gives Caddy the host's `tailscaled` socket, so Caddy fetches the `*.ts.net` certificate from Tailscale. No Let's Encrypt, no open port 80.

## Removing a trader

Remove the user (or their device) in the Tailscale admin console. Their access ends at once. Also rotate `STONKS_API_TOKEN` if they knew it (see `docs/deploy.md`, Secrets).

## Alternative: Cloudflare Tunnel

If traders cannot install Tailscale, run `cloudflared` as an extra compose service pointing at `http://api:8000` (with `httpHostHeader: localhost`, like Caddy's `header_up`), drop the Caddy service, and put Cloudflare Access (email one-time codes) in front. The server still opens no port. Tailscale remains the default because it also covers SSH and CI.
