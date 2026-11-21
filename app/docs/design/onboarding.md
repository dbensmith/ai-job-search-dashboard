# Dashboard onboarding for non-technical users

Status: plan only, no code. Scope: getting a new user from "I have tracker records" to "I can see my dashboard". It does not cover the Claude skills, CV tooling or LaTeX; those are separate plans.

Companion docs: [data pipeline](data-pipeline.md), [secrets management](secrets-management.md), [deploying](../../DEPLOY.md).

## 1. Today's path to a dashboard

| Path | What the user does | Hurdles |
|---|---|---|
| Local build | Install Python, run `python tools/build_report.py`, open `site/index.html` | One terminal command; no accounts |
| Demo | Open the GitHub Pages site | None, but it shows sample data only |
| Private hosted view | Cloudflare account and a domain on Cloudflare nameservers, enable R2 and Zero Trust in the dashboard (may ask for a card), mint an API token with seven permissions, run `mise run setup`, set repo variables, merge the plan PR, route the guest alias through Email Routing | About 10 manual steps: 1 terminal tool, 1 token, 2 dashboard-only sign-ups, 1 domain/DNS change |

The local build is already cheap. The hosted view is the hurdle: a non-technical user cannot finish it unaided.

Target for the whole dashboard journey: no terminal, no pasted secret, no domain, at most two sign-ins, under 10 minutes.

## 2. Requirements

1. The default path needs no hosting, no domain and no secret.
2. Any credential is created by an "Allow" click, never copied by hand.
3. The user's records never enter the public repo or any public site.
4. The page works on a phone and opens offline.
5. Advanced hosting stays available and is not removed.

## 3. Constraints that settle the options

- GitHub Pages on the Free plan serves public repositories only, and a Pages site is always public. Pages therefore carries the fictional demo and never a user's data.
- A private hosted view needs access control. Cloudflare Access is the current answer and it needs a zone (a domain) on the account.
- The page is one static, deterministic, self-contained HTML file with inline SVG charts, so it can be opened from disk, attached to an email or hosted anywhere.

## 4. Options

### A. Local report as the default (recommended)
The user asks Claude to build the dashboard; Claude runs `build_report.py` and hands over `index.html` to double-click. A reminder offers a rebuild after each batch of changes.
- Hurdles: none beyond having Claude open.
- Cost: nothing. Privacy: best, data stays on the device.
- Weakness: no link to open on another device; the user re-opens or re-sends the file.

### B. Guided private hosting (advanced upgrade)
Keep the OpenTofu and Access path, but have Claude drive it as a conversation: it checks preflight, opens the pre-filled token link, explains each dashboard-only sign-up, and verifies that anonymous requests are blocked.
- Hurdles: Cloudflare account, domain, one token. Still a real hurdle.
- Use it when someone wants the same URL on phone and laptop.

### C. Hosted service run by the maintainer
Not recommended. It would mean holding strangers' job-search data (employers, pay, contacts), with the legal and trust duties that come with it, and it contradicts the goal of publishing the method and not the data.

## 5. Recommendation

Make **A the default** and keep **B as an opt-in upgrade**. Do not build C. Each user owns their data; the project hosts nothing except the public demo.

## 6. Milestones

One PR each, off `master`, with a check-in between.

1. **Local default.** Document the zero-hosting path first in the README and SETUP; add a `--open` flag to `build_report.py` that opens the result in the default browser. Add a test that the output has no network references (no external script, font or image).
2. **Empty-state first view.** With no records, the page shows a short "how to add your first role" panel and offers the sample data, instead of blank charts.
3. **Refresh reminder.** The submit and outcome tools print a one-line rebuild hint, so the page does not go stale unnoticed.
4. **Guided hosting.** A Claude-led walkthrough for option B: preflight, token link, dashboard-only sign-ups, anonymous-access probe. No new infrastructure; it wraps what `DEPLOY.md` already describes.
5. **Domain-free private view (investigate).** Check whether Cloudflare Access can protect a Worker on `workers.dev` for an account without a zone. If it can, option B loses its domain hurdle; if not, record the limit in `DEPLOY.md`.
6. **Usability test.** Three non-technical users, timed. Ship only if each reaches a working dashboard unaided in under 10 minutes on the default path.

## 7. Risks and open questions

- A stale local page can mislead; milestone 3 mitigates it.
- Cloudflare's dashboard-only steps change over time. The guided walkthrough must read current docs and not rely on remembered screens.
- Milestone 5 is unverified: nothing here assumes Access works without a domain.

## 8. Decision needed

Should the zero-hosting local report be the default for new users, with Cloudflare kept as an advanced option? Recommended: yes.
