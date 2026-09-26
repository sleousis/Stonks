output "ipv4" {
  description = "Public IPv4 (SSH target in public mode; DNS A record for the domain)."
  value       = hcloud_server.this.ipv4_address
}

output "ipv6" {
  description = "Public IPv6."
  value       = hcloud_server.this.ipv6_address
}

output "tailscale_name" {
  description = "Tailscale machine name (SSH and console target in Tailscale mode)."
  value       = nonsensitive(local.tailscale) ? var.name : null
}

output "volume_device" {
  description = "Block device of the data volume."
  value       = hcloud_volume.data.linux_device
}
