# Bootstrap: the R2 bucket that holds OpenTofu state, for this stack and for ../main.tf. It can't live in ../main.tf,
# because a backend must exist before `tofu init`. deploy.mjs applies this once, when the bucket is missing, with local
# state, then migrates that state into the bucket it just created (the pattern of cloudposse/tfstate-backend, which
# only supports AWS). After that the bucket is managed like everything else and protected from deletion.
terraform {
  required_version = ">= 1.10"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.29.0"
    }
  }
}

# Same backend as ../main.tf, under its own key. deploy.mjs supplies bucket and credentials (see ../main.tf).
terraform {
  backend "s3" {
    key                         = "ai-job-search/bootstrap.tfstate"
    region                      = "auto"
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
    use_path_style              = true
    use_lockfile                = true
  }
}

provider "cloudflare" {}

variable "account_id" {
  type = string
}

variable "state_bucket" {
  type    = string
  default = "ai-job-search-tfstate"
}

resource "cloudflare_r2_bucket" "state" {
  account_id = var.account_id
  name       = var.state_bucket

  # Holds every stack's state. A plan that would destroy it fails.
  lifecycle {
    prevent_destroy = true
  }
}
