# Dashboard onboarding

Status: plan only, no code. Scope: the dashboard page that `tools/build_report.py` generates from a user's own tracker records. Hosting is an optional adapter (see the design rule below); accounts and other tooling are out of scope here; see [data pipeline](data-pipeline.md) and [deploying](../../DEPLOY.md) for those.

## Goal

A new user can build the dashboard from their own records and open it, with no accounts, no domain and no secrets.

## Design rule: local first, hostable anywhere

The local build is the product. It must be a plain static folder that any static host can serve unchanged, so hosting is a thin, optional adapter and never a dependency.

- Output is `site/` only: relative links, no server, no runtime API, no build-time vendor SDK.
- Records stay outside the page logic: the same page is built from sample data or real data by changing only the input paths.
- The build takes no hosting settings and reads no secrets.
- Adapters (Cloudflare Workers assets with Access today, GitHub Pages for the demo, or any other static host or object store) only upload `site/` and add access control. Cloudflare stays an optional upgrade.
- Access control is the host's job. The page assumes it is served privately or not at all.

## Today

`python tools/build_report.py` writes one self-contained page, `site/index.html`, deterministically and with no network. Opening it from disk works. The gaps are in the first-run experience, not the generator.

## Milestones

One PR each, off `master`.

1. **Document the local path first.** README and SETUP lead with building and opening the report locally; hosting is described as optional.
2. **`--open` flag.** `build_report.py --open` opens the result in the default browser.
3. **No-network test.** A test asserts the output references no external script, font or image, so it keeps working from disk and offline.
4. **Empty-state view.** With no records, the page shows a short "add your first role" panel and a pointer to the sample data, instead of blank charts.
5. **Host-neutral output check.** A test asserts `site/` is self-contained and relocatable: relative paths only, no absolute URLs to a host, works when served from a subpath.
6. **Rebuild hint.** The tracker submit and outcome tools print a one-line reminder to rebuild, so the page does not go stale unnoticed.

## Out of scope

Guided hosting setup, hosting without a domain, a packaged plugin, replacing LaTeX and user testing. Each would need its own plan if wanted.
