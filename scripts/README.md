# Session setup

For a fresh agent session (Claude cloud environment, Remote Control, dev box). One script, one set of mise tasks (`mise.toml`).

| Command | What it does |
|---|---|
| `bash scripts/session-setup.sh` | Installs mise if missing (version from the CI workflow, sha256 from that release), then `mise run session:setup`: Python from `.python-version` with the hash-pinned `requirements-ci.txt` (into `.session/`), the dashboard's pinned tools from `app/mise.lock` (Node, OpenTofu, the 1Password CLI) and `npm ci`. Idempotent. |
| `mise run check` | Everything dashboard CI checks without credentials: PII guard, Python unit tests for the dashboard files, dashboard fmt/validate/tests. |
| `mise run session:teardown` | Local dev only, run by hand: removes `.session/`, the `.terraform` caches, `app/dist`, pycache. Never touches secrets (`app/.env*`, tokens), OpenTofu state or the mise/uv caches. Cloud sessions need no teardown, the container is discarded. |

## Claude cloud environment

Setup script (this exact line, it is what the environment runs):

```
bash ai-job-search/scripts/session-setup.sh
```

The cloud environment runs the setup script in the session's working directory, which is the *parent* of the clone (the repo is cloned as `ai-job-search/`), not inside it. So the path is relative to that parent. `bash "$(git rev-parse --show-toplevel)/scripts/session-setup.sh"` does not work there: git finds no repository from the parent and the command fails. The script itself does not care where it is called from, so inside the clone (local or Remote Control) use `bash scripts/session-setup.sh`. If the repo is renamed, change `ai-job-search` in the environment setting.

Allowed domains (most are covered by "Allow package managers"):
`github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com`, `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `nodejs.org`, `registry.opentofu.org`.

## Bumping mise

The mise version is read from the `version:` line of mise-action in `.github/workflows/deploy-dashboard.yml`, which Renovate bumps; the sha256 is taken from that release. Nothing here needs editing on a bump.
