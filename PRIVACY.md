# Privacy: no personal data in this repository

This repository is public-safe by design. It holds the method: code, templates, prompts and runbooks. It never holds a real person's profile, CV, contacts, pay, employers' internal details or job-search records.

## The rules

1. Tracked files MUST NOT contain personal data. The template files (`CLAUDE.template.md`, the `job-application-assistant` reference files, `cv/main_example.tex` and so on) keep their `[PLACEHOLDER]` tokens.
2. Your real profile lives in the private overlay, which git ignores:
   - `CLAUDE.md`: the candidate profile. It is not tracked. `/setup` creates it from the tracked `CLAUDE.template.md`; in a fresh clone, copy yours in or run `cp CLAUDE.template.md CLAUDE.md`.
   - `.private/`: personalized copies of the skill reference files, your master CV source, voice guide and anything else personal.
   Keep the overlay somewhere private (a synced folder, a private store) and copy it into a fresh clone.
3. Generated output (`cv/main_*`, `cover_letters/cover_*`, `documents/**`, tracker exports, PDFs) is ignored and MUST NOT be force-added.
4. Commit messages, PR titles and PR descriptions are public too. Describe the change, never the person.
5. Tests and examples use fictional data (`example.com`, 555 numbers, made-up companies).

## The guard

`tools/pii_guard.py` enforces this in three places:

| Where | What it checks |
|---|---|
| `pre-commit` and `commit-msg` hooks (`.githooks/`) | staged lines and the commit message, before a commit exists |
| CI (`ci.yml` and `dashboard-ci.yml`, plus `pii-guard.yml` for the paths `ci.yml` skips) | every line and commit message a PR adds, plus every tracked file |
| `mise run pii-scan` (from `app/`) or `python tools/pii_guard.py --all` | every tracked file, on demand |

Enable the hooks once per clone: `python tools/pii_guard.py --install-hooks` (or `mise run pii-hooks`). Claude Code sessions enable them automatically through the `SessionStart` hook in `.claude/settings.json`.

Generic rules are always on: email addresses, phone numbers, credentials, 32-hex account ids, Cloudflare hostnames, street addresses and postal codes, home-directory paths, and paths reserved for personal output.

### The maintainer deny list

Generic rules cannot know your name, employers or city. Those go in a deny list of regular expressions, one per line, case-insensitive. The deny list is itself personal data, so it MUST NOT be tracked. The guard reads it from:

- `PII_DENYLIST` (environment variable; in CI, a repository secret of the same name),
- the file named by `PII_DENYLIST_FILE`,
- `.pii-denylist` at the repo root (gitignored; the guard fails if it is ever tracked).

Without a deny list the guard warns and runs the generic rules only.

### False positives

Findings are reported as `path:line` and rule with the value masked, so logs stay clean. For a genuine false positive, add a regex for the value to `.pii-allow` (tracked, fixtures and placeholders only) or put `pii-guard: allow` on the line.

## If personal data is committed anyway

Deleting it in a later commit is not enough: it stays in history. Stop, do not push, and rewrite the commit. If it was pushed, treat it as published: rewrite history with `git filter-repo`, force-push, and ask GitHub Support to purge cached views and dangling commits.
