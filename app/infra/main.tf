# Every Cloudflare resource of the dashboard: Access (allowed people), the Worker that serves the static site/ folder as
# assets, and its custom domain. No database and no API: the site is built by tools/build_report.py. Authenticate with
# CLOUDFLARE_API_TOKEN in the environment; it is never stored in state.
terraform {
  required_version = ">= 1.10"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.29.0"
    }
  }
}

# State lives in an R2 bucket (S3-compatible) so any runner can plan and apply. The bucket comes from
# -backend-config=bucket=..., the endpoint from AWS_ENDPOINT_URL_S3 and the credentials from AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY; deploy.mjs creates the bucket and derives the credentials from the Cloudflare API token.
# It holds resource ids (the state is still encrypted client-side, see TF_ENCRYPTION).
terraform {
  backend "s3" {
    key                         = "ai-job-search/terraform.tfstate"
    region                      = "auto"
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
    use_path_style              = true
    # Locking: a lock file next to the state, written with a conditional PUT (If-None-Match), which R2 supports.
    use_lockfile = true
  }
}

provider "cloudflare" {}

variable "account_id" {
  type = string
}

variable "hostname" {
  type        = string
  description = "Public hostname of the app, for example dashboard.example.com"
}

variable "manage_org" {
  type        = bool
  default     = false
  description = "Create the Zero Trust organization (only when the account has none)"
}

variable "team_name" {
  type    = string
  default = ""
}

variable "team_domain" {
  type        = string
  default     = ""
  description = "Existing team domain when this configuration does not manage the organization"
}

variable "allowed_emails" {
  type        = list(string)
  description = "People Access lets through"
}

variable "zone_id" {
  type        = string
  description = "Zone that holds the hostname (deploy.mjs finds it)"
}

variable "site_dir" {
  type        = string
  default     = "../../site"
  description = "The static site built by tools/build_report.py, relative to infra/"
}

variable "compatibility_date" {
  type        = string
  description = "Workers runtime date; deploy.mjs reads it from wrangler.toml so local dev and production match"
}

# Zero Trust must exist before Access applications. Created here only when the account has no team yet; once
# created it stays managed (deploy.mjs keeps manage_org true while it is in state) and cannot be destroyed by accident.
resource "cloudflare_zero_trust_organization" "org" {
  count       = var.manage_org ? 1 : 0
  account_id  = var.account_id
  name        = var.team_name
  auth_domain = "${var.team_name}.cloudflareaccess.com"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "allow" {
  account_id = var.account_id
  name       = "AI job search dashboard: allowed people"
  decision   = "allow"
  include = [for e in var.allowed_emails : {
    email = { email = e }
  }]
}

resource "cloudflare_zero_trust_access_application" "dashboard" {
  account_id       = var.account_id
  name             = "AI job search dashboard"
  domain           = var.hostname
  type             = "self_hosted"
  session_duration = "24h"
  policies = [{
    id         = cloudflare_zero_trust_access_policy.allow.id
    precedence = 1
  }]
  depends_on = [cloudflare_zero_trust_organization.org]
}

# The Worker only serves the static folder: Access (above) is the front door, so the Worker needs no code of its own
# beyond a 404 for paths that are not files. Page changes show in the plan through the assets manifest.
resource "cloudflare_workers_script" "dashboard" {
  account_id         = var.account_id
  script_name        = "ai-job-search-dashboard"
  main_module        = "worker.js"
  content            = "export default { fetch: () => new Response('Not found', { status: 404 }) };"
  compatibility_date = var.compatibility_date

  assets = {
    directory = "${path.module}/${var.site_dir}"
  }

  bindings = [
    { type = "assets", name = "ASSETS" },
  ]
}

# Only reachable on the custom domain, which sits behind Access: no workers.dev URL and no preview URLs.
resource "cloudflare_workers_script_subdomain" "dashboard" {
  account_id       = var.account_id
  script_name      = cloudflare_workers_script.dashboard.script_name
  enabled          = false
  previews_enabled = false
}

resource "cloudflare_workers_custom_domain" "dashboard" {
  account_id = var.account_id
  zone_id    = var.zone_id
  hostname   = var.hostname
  service    = cloudflare_workers_script.dashboard.script_name
  # Access protects the hostname before the Worker answers on it.
  depends_on = [cloudflare_zero_trust_access_application.dashboard]
}

output "team_domain" {
  value = var.manage_org ? cloudflare_zero_trust_organization.org[0].auth_domain : var.team_domain
}

output "access_aud" {
  value = cloudflare_zero_trust_access_application.dashboard.aud
}
