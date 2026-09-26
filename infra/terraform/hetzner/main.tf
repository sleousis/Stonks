# One small always-on VM for Stonks (Phase 14.1).
#
#   hcloud_firewall --> hcloud_server (Ubuntu + cloud-init) <-- hcloud_volume (/srv/stonks)
#
# Tailscale mode (tailscale_auth_key set): the firewall has no inbound rules at
# all; SSH, the console and the API are reached over the tailnet.
# Public mode: SSH from ssh_allowed_cidrs, and 80/443 when public_https = true.

locals {
  tailscale = var.tailscale_auth_key != ""

  ssh_rules = local.tailscale ? [] : [
    for cidr in var.ssh_allowed_cidrs : { port = "22", protocol = "tcp", source_ips = [cidr] }
  ]

  https_rules = var.public_https ? [
    { port = "80", protocol = "tcp", source_ips = ["0.0.0.0/0", "::/0"] },
    { port = "443", protocol = "tcp", source_ips = ["0.0.0.0/0", "::/0"] },
    { port = "443", protocol = "udp", source_ips = ["0.0.0.0/0", "::/0"] },
  ] : []
}

resource "hcloud_ssh_key" "admin" {
  name       = "${var.name}-admin"
  public_key = var.admin_ssh_public_key
}

resource "hcloud_firewall" "this" {
  name = var.name

  dynamic "rule" {
    for_each = concat(local.ssh_rules, local.https_rules)
    content {
      direction  = "in"
      protocol   = rule.value.protocol
      port       = rule.value.port
      source_ips = rule.value.source_ips
    }
  }
}

resource "hcloud_volume" "data" {
  name     = "${var.name}-data"
  size     = var.volume_size_gb
  location = var.location
  format   = "ext4"
}

resource "hcloud_server" "this" {
  name         = var.name
  server_type  = var.server_type
  image        = var.image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.admin.id]
  firewall_ids = [hcloud_firewall.this.id]
  backups      = false # off-server backups are restic (deploy/backup/)

  user_data = templatefile("${path.module}/../../../deploy/cloud-init.yaml", {
    admin_ssh_public_key  = var.admin_ssh_public_key
    deploy_ssh_public_key = var.deploy_ssh_public_key
    volume_device         = hcloud_volume.data.linux_device
    tailscale_auth_key    = var.tailscale_auth_key
    tailscale_hostname    = var.name
  })

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  lifecycle {
    # Editing cloud-init later must not rebuild a running server.
    ignore_changes = [user_data, ssh_keys]
  }
}

resource "hcloud_volume_attachment" "data" {
  volume_id = hcloud_volume.data.id
  server_id = hcloud_server.this.id
  automount = false # cloud-init mounts it by label at /srv/stonks
}
