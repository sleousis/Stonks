terraform {
  required_version = ">= 1.6"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.50"
    }
  }
}

# Pass the token as TF_VAR_hcloud_token; never write it into a file.
provider "hcloud" {
  token = var.hcloud_token
}
