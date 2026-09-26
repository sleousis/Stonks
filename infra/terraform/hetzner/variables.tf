variable "hcloud_token" {
  description = "Hetzner Cloud API token (read/write). Pass via TF_VAR_hcloud_token; never commit it."
  type        = string
  sensitive   = true
}

variable "name" {
  description = "Server name; also the Tailscale machine name."
  type        = string
  default     = "stonks"
}

variable "location" {
  description = "Hetzner location: nbg1, fsn1, hel1 (EU), ash, hil (US), sin."
  type        = string
  default     = "nbg1"
}

variable "server_type" {
  description = "Server size. 2 vCPU / 4 GB is enough for several traders; list current names with `hcloud server-type list`."
  type        = string
  default     = "cx22"
}

variable "image" {
  description = "OS image."
  type        = string
  default     = "ubuntu-24.04"
}

variable "volume_size_gb" {
  description = "Block volume for /srv/stonks (lake, Parquet bars, state, artifacts, snapshots)."
  type        = number
  default     = 20
}

variable "admin_ssh_public_key" {
  description = "Your SSH public key (user `admin`, sudo)."
  type        = string
}

variable "deploy_ssh_public_key" {
  description = "Public half of the CI deploy key (user `deploy`, docker group only)."
  type        = string
}

variable "tailscale_auth_key" {
  description = "Tailscale auth key (one-off, tagged, pre-approved). Empty = public mode."
  type        = string
  default     = ""
  sensitive   = true
}

variable "ssh_allowed_cidrs" {
  description = "Source ranges allowed to reach SSH from the internet. Ignored in Tailscale mode (no port is opened)."
  type        = list(string)
  default     = []
}

variable "public_https" {
  description = "Open 80/443 to the internet (public mode with a real domain). Keep false with Tailscale."
  type        = bool
  default     = false
}
