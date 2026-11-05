# The home-network test lab: no cloud. Every step is a script run over SSH from
# this Mac (scripts/), so Terraform supplies only the inventory, the order,
# change detection and teardown. See README.md.
terraform {
  required_version = ">= 1.6"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}
