# Dashboard (static site on Cloudflare Workers assets)

A read-only dashboard of the job search: found, checked, drafted, applied, replies, interviews, decisions, with a dated timeline and KPIs. It is a static folder, `site/`, written by `python tools/build_report.py [--csv FILE] [--events FILE] [--out site]` from the tracker CSV files. There is no database, no API and no editing in the browser; change the records and rebuild.

- `infra/` OpenTofu: the Worker that serves `site/` as static assets, its custom domain, and the Cloudflare Access application with an allowed-emails policy. `infra/bootstrap/` creates the R2 bucket that holds the OpenTofu state.
- `deploy/` the plan-first deploy script and the settings loader (environment variables or a 1Password vault)
- `wrangler.toml` local preview (`npm run preview`) and the bundle check (`npm run bundle`); production is OpenTofu's
- `../tracker/examples/` fictional sample data (the public demo is built from it)
- `docs/design/` [data pipeline](docs/design/data-pipeline.md), [secrets management](docs/design/secrets-management.md), [onboarding plan](docs/design/onboarding.md)

## Security model

1. **Cloudflare Access** is the front door: a self-hosted application on your hostname with an allow policy for your emails. Nothing is served to anyone else.
2. `workers_dev` and preview URLs are off, so there is no unauthenticated URL. After every apply the deploy checks that an anonymous request is blocked and fails the run if it is not.
3. The site is static: no scripts talk to a backend, and the records leave the repo only as the built pages behind Access.
4. No secrets are stored in the repo. Deploy credentials are plain environment variables (GitHub Actions secrets in CI), or are read from a 1Password vault at run time when `OP_VAULT` is set (`op run --env-file app/1password.env`).
5. The public fork never deploys: it validates, and publishes a demo from sample data to GitHub Pages.

## Deploy

Scripted, plan-first deploys through GitHub Actions: [DEPLOY.md](DEPLOY.md).

## Local development

```bash
python tools/build_report.py --events tracker/examples/events.csv --out site   # from the repo root
cd app && npm ci
npm run preview      # wrangler dev serves ../site at http://localhost:8787
npm test             # deploy script tests (fake Cloudflare API)
```
