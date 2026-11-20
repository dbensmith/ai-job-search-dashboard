# Dashboard data pipeline

Status: implemented. The dashboard is a static site: no database, no API, no editing in the browser.

```mermaid
flowchart LR
  CSV["job_search_tracker.csv<br/>+ events CSV"] --> BUILD["python tools/build_report.py"]
  BUILD --> SITE["site/ (index.html)"]
  SITE -- "demo, sample data" --> PAGES["GitHub Pages"]
  SITE -- "OpenTofu uploads as Worker assets" --> WORKER["Cloudflare Worker"]
  ACCESS["Cloudflare Access<br/>(allowed emails)"] --> WORKER
```

## Layers and owners

| Layer | Owner | Changes by |
|---|---|---|
| The records (applications, events) | the tracker CSV files, written by the tracker tools and Claude skills | commits to the data repo |
| The page | `tools/build_report.py`: deterministic, same input gives the same `site/` | `python tools/build_report.py [--csv FILE] [--events FILE] [--out site]` |
| Hosting | OpenTofu (`app/infra/main.tf`): Worker with the static assets, custom domain, Access application and allowed-emails policy | plan and apply (`app/DEPLOY.md`) |

Nothing is written back from the page. To change a record, change the CSV and rebuild; the next deploy replaces the site.

## Where it runs

- **Private deployment:** `deploy-dashboard.yml` builds `site/` from the real data and, only when the repository variable `DASHBOARD_DEPLOY` is `true`, plans and applies it. Cloudflare Access is the only front door.
- **Public fork:** `deploy-dashboard.yml` only validates (tofu fmt/validate, node tests, a build from sample data). `pages.yml` publishes a demo built from `tracker/examples/events.csv` to GitHub Pages. Sample data only.
