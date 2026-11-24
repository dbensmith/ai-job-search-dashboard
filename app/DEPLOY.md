# Deploying the dashboard

Design: [Secrets Management](docs/design/secrets-management.md) (environment variables or 1Password) and [Dashboard data pipeline](docs/design/data-pipeline.md).

Everything runs in GitHub Actions (`.github/workflows/deploy-dashboard.yml`); you press nothing after setup. **Deploys are off until the repository variable `DASHBOARD_DEPLOY` is `true`.** Without it (a public fork, or a repo with no Cloudflare account yet) the workflow only validates: `tofu fmt`/`validate`, the node tests, and a build of the site from the sample data. It reads no secret.

| When | What runs | Changes anything? |
| --- | --- | --- |
| `DASHBOARD_DEPLOY` not `true`, or no deploy settings at all | tests, `tofu fmt`/`validate`, a site build from sample data; the job summary says how to turn deploys on | No, and the check stays green |
| Pull request (same repo, deploys on) | the same, a site build from the real data, preflight, **`tofu plan`** posted as a PR comment for every commit that changes `app/`, `tools/build_report.py`, `tracker/` or the workflow | No. Creates nothing, not even the state bucket |
| Fork or Dependabot PR | tests, `tofu fmt`/`validate` only (GitHub withholds secrets from them) | No |
| Push to `master` (deploys on) | the same, then **apply** if the repository variable `DEPLOY_AUTO_APPLY` is `true`; otherwise plan only | Only with auto-apply on |
| Manual run (Actions, *Dashboard*) | mode `plan` or `apply` (plans, then applies that plan) | Only in `apply` |

Safeguards: apply refuses any plan that deletes or replaces a resource, the site is probed afterwards and the run fails unless Access blocks anonymous requests, and the hostname and emails are replaced in the PR comment and summary. GitHub Free private repositories have no required-reviewer gate for environments, so the approval step is: read the PR comment, merge, and keep `DEPLOY_AUTO_APPLY` off until you trust it.

## Set up (any starting point)

1. **Token.** Open [this pre-filled link](https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys=%5B%7B%22key%22%3A%22workers_scripts%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22workers_r2%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22access%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22access_acct%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22workers_routes%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22dns%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22zone%22%2C%22type%22%3A%22read%22%7D%5D&accountId=%2A&zoneId=all&name=ai-job-search-dashboard-deploy), check the seven permissions in the table below, Continue, Create Token, copy it.
2. **Settings (once per deployment).** `cd app && mise install && mise run setup` (flags: `--source env|1password --vault V --hostname H --emails E`; anything missing is asked, secrets hidden). `env` stores the settings as GitHub secrets; `1password` stores them in a vault and sets `OP_VAULT`. Either way setup also generates the OpenTofu state encryption key, and switching later with another run removes the old source. Details: [Secrets Management](docs/design/secrets-management.md).

   Setup also adds a guest sign-in alias `job-search-guest@<your zone>` (zone looked up from the hostname; change the part before the `@` with `--guest-local`, or pass an empty value to skip). Access emails a one-time PIN to allowed addresses, so add a Cloudflare Email Routing route (domain, Email, Email Routing, Routes) forwarding the alias to your inbox; you pass the PIN on.
3. **Turn deploys on.** Set the repository variable `DASHBOARD_DEPLOY` to `true`, then open a PR (or run *Dashboard* manually, mode `plan`). If anything is missing or the token lacks a permission, the run fails with a list naming each item and a link to where to set it; fix and re-run. When it is green, read the plan, then merge (with auto-apply on) or run mode `apply`.

Only if the preflight says so: enable **R2** (Dashboard, R2 Object Storage, free plan) or **Zero Trust** (Dashboard, Zero Trust, Free plan). Both are dashboard-only sign-ups and may ask for a payment method; neither is charged within free limits. Other things that cannot be automated: minting the first token and moving a domain's nameservers to Cloudflare.

## Deploying from a repository that holds the records

The public fork never sees real records. A private repository that has them calls the composite action `.github/actions/dashboard` from a small workflow of its own (path-filtered to the records, plus a manual run), so the logs, the data and the settings stay private while all of the build and deploy logic stays here:

```yaml
jobs:
  dashboard:
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@<sha> # records, no credentials kept
        with: { persist-credentials: false }
      - uses: dbensmith/ai-job-search-dashboard/.github/actions/dashboard@<sha> # master
        with:
          data-dir: .
          cloud: ${{ vars.DASHBOARD_DEPLOY == 'true' }}
          mode: plan
          # op-vault / cloudflare-api-token / tf-encryption / hostname / allowed-emails, as in deploy-dashboard.yml
```

The action reads `tracker/events.csv` and `documents/applications/` under `data-dir` (the root `job_search_tracker.csv` that upstream's report reads is derived from the log), builds `site/` and runs the same preflight, plan and apply as this repo's own workflow. Without `data-dir` it builds the demo from `tracker/examples/events.csv`. It fails if `data-dir` is set and the calling repository is not private.

## Local development (mise)

CI runs the same `mise` tasks you can run on a dev box, in a container, or in an agent sandbox such as Claude Code, so they cannot drift. Tools are pinned in `app/mise.toml` (Node, OpenTofu, the 1Password CLI; checksums in `mise.lock`; wrangler is pinned by `package-lock.json`).

```
# one time: install mise (https://mise.jdx.dev), then
cd app
mise trust && mise install
mise run check                    # tofu fmt + validate + unit tests: no credentials, no Cloudflare
```

The tasks that talk to Cloudflare read the settings from `app/.env` (or the environment), or from 1Password when `OP_VAULT` is set; see [Secrets Management](docs/design/secrets-management.md). Values are never printed. They need `site/` first: `mise run site`.

| Task | Same as CI step | Needs | Changes anything? |
| --- | --- | --- | --- |
| `mise run check` (`fmt`, `validate`, `test`) | tofu fmt/validate, unit tests | nothing; first `install`/`validate` download packages once | No |
| `mise run site` | Build the site | Python | Writes `site/` |
| `mise run settings` | | nothing | No: shows the source and which names are set |
| `mise run preflight` (`preflight-names`, `preflight-token`) | Preflight | the three settings; the token step calls Cloudflare | No, read-only |
| `mise run plan` | PR dry run | the three settings, Cloudflare reachable, `site/` | No: nothing created, not even the state bucket |
| `mise run plan-save` | manual `plan` | same | Creates only the empty state bucket, saves the plan |
| `mise run apply --confirm-apply` | manual `apply` / auto-apply | same | **Yes.** Refuses without the flag. Plan, then apply |

Agent sandbox note: `mise run check` works with no secrets and no Cloudflare access, so an agent can verify any change. Everything else needs the token and outbound HTTPS to `api.cloudflare.com` (and the R2 endpoint for state). Do not hand an agent the token if it should not be able to apply, because the token itself can change Cloudflare; the `--confirm-apply` flag is a guard against accidents, not a permission boundary. In 1Password mode, give the sandbox `OP_VAULT` and its own read-only service account token as `OP_SERVICE_ACCOUNT_TOKEN`.

Updating tools: Renovate bumps `mise.toml`, `mise.lock` (via its mise manager) and the mise version in the workflows (a hinted `version:` line) in its normal automerged group. By hand: change the version, run `mise lock`, and keep the workflows' `version:` in step.

## Which scenario am I in?

| You have | What happens |
| --- | --- |
| New GitHub repo, new Cloudflare account (domain added) | Creates the state bucket, Zero Trust team (named after the zone, or `CLOUDFLARE_ACCESS_TEAM_NAME`), Access policy and application, Worker and custom domain |
| Zero Trust team already exists | Reused as is, never modified |
| A DNS record already exists for the hostname | Preflight warns. Delete that record (or pick another hostname) before applying, or the custom-domain step fails |
| Token missing a permission, R2 or Zero Trust not enabled | Preflight fails before anything runs and names the permission |
| Keeping all settings in 1Password | See [Secrets Management](docs/design/secrets-management.md). Only `OP_SERVICE_ACCOUNT_TOKEN` is stored in GitHub |
| Not using Actions | Run the same tasks locally with mise, see *Local development* |

Optional repository variables: `CLOUDFLARE_ACCOUNT_ID` (default: the account that owns the zone), `CLOUDFLARE_ACCESS_TEAM_NAME` (only used when the account has no Zero Trust team; must be unused across Cloudflare).

## State encryption

State and plans are always encrypted client-side (OpenTofu reads `TF_ENCRYPTION`: PBKDF2 passphrase, AES-GCM, enforced), since R2 has no customer-managed keys. Setup generates the key once and keeps it on re-runs and when switching methods. Losing it loses the state; re-import the resources with `tofu import`.

### Token permissions to confirm in step 1

The link pre-fills them; Cloudflare silently drops any it does not recognise, so check all seven are listed.

| Scope | Permission |
| --- | --- |
| Account | Workers Scripts: Edit |
| Account | Workers R2 Storage: Edit |
| Account | Access: Apps and Policies: Edit |
| Account | Access: Organizations, Identity Providers, and Groups: Edit |
| Zone | Workers Routes: Edit |
| Zone | DNS: Edit |
| Zone | Zone: Read |

You may narrow the token to your one account and the one zone on the same screen.

### What the deploy creates for you

| Thing | Created by |
| --- | --- |
| Cloudflare account id, Zero Trust team name | read from the hostname's zone (no setting needed) |
| R2 bucket for OpenTofu state | OpenTofu bootstrap stack (`infra/bootstrap`), applied only when the bucket is missing; its state then moves into that bucket, and state is locked with a lock file (`use_lockfile`) |
| The bucket's S3 credentials | derived from the token |
| Zero Trust team (if none), Access policy and application, the Worker with the `site/` assets, its custom domain | OpenTofu (`infra/main.tf`, provider pinned) |
| `site/` | `python tools/build_report.py` (before each plan) |

## Free plan notes

Everything fits the Cloudflare free tier: Workers static assets (free and unlimited requests), Access (up to 50 users), a custom domain on a free zone, R2 (10 GB).

## Rollback

Revert the commit and apply; OpenTofu owns the Worker, so a dashboard change is undone by the next apply. The data lives in the tracker CSV files, never in Cloudflare.

## What was and wasn't tested

`deploy.mjs` (preflight, bootstrap, dry run, destroy guard, redaction, the missing-site guard) is unit-tested with a fake Cloudflare API, `tofu` and `wrangler`. `infra/main.tf` and `infra/bootstrap/main.tf` pass `tofu fmt` and `tofu validate`. Not tested, because nothing here has Cloudflare credentials: the workflow against a real account, the PR comment step, the token link, the R2 backend and the bootstrap's state migration, the Zero Trust organization against a live account, the Worker upload, and the `op` calls. The first PR run is the real check; failures name the step and the setting.
