# Tracker events log

`tracker/events.csv` is the source of truth for your job search: one dated, append-only row per action. Roles, their status and history are derived from it. `job_search_tracker.csv` (the file upstream's commands read) is regenerated from it after every write, so `/rank`, `/outcome` and `/html-report` keep working. Rows are never rewritten; git history is the backstop.

The file holds your own data, so it is gitignored here. A fictional sample lives in `examples/events.csv`; the demo dashboard is built from it.

```
date,id,type,company,role,field,value,note,artifact,commit
```

| Type | Meaning |
|---|---|
| `found` | creates the role (`company` and `role` required, once per `id`) |
| `set` | `field`=`value`, one of the editable fields in `schema.json` (latest wins; empty value clears) |
| `validated` | the posting was checked on the employer's own career page |
| `decided_to_apply`, `drafted`, `applied` | the path to a submission |
| `outreach_sent`, `response_received`, `screen_scheduled`, `follow_up_sent`, `role_closed` | dated steps that do not change the status (`role_closed` stamps `closed_on`) |
| `interview` | `value` = stage (`recruiter_screen`, `hiring_manager`, `technical`, `panel`, `final`) |
| `offer`, `hired`, `rejected`, `no_response`, `offer_declined`, `withdrawn` | outcomes |
| `skipped` | `value` = reason (`location`, `onsite`, `work_auth`, `on_call`, `level`, `comp`, `closed`, ...) |

`artifact` is the repo path of the evidence (submitted PDF, posting snapshot, submission record) and `commit` the commit that holds it, so the dashboard links straight to it. `tools/tracker_submit.py` fills `commit` from HEAD.

Rules enforced in code (`python3 tools/tracker.py validate`, run by the unit tests): the status path in `schema.json`, dates never go backwards per role, `skipped` needs a reason, and a role cannot leave `found` without a `validated` event and the employer's posting URL (a referral needs only `validated`).

## Commands

```bash
python3 tools/tracker.py add --company "Acme" --role "Security Architect" --url https://... --fit 75
python3 tools/tracker.py event <id> validated
python3 tools/tracker.py event <id> interview --value recruiter_screen --note "30 min"
python3 tools/tracker.py set <id> comp_min=180000 comp_max=210000 comp_currency=CAD
python3 tools/tracker.py list --open
python3 tools/tracker_submit.py submit <id> --artifact documents/applications/<pack>/cv.pdf
python3 tools/tracker_submit.py check-packs documents/applications
python3 tools/build_report.py --out site       # the dashboard: site/index.html
```

**Recording a submission is one scripted step.** Never mark an application `applied` by editing a CSV: run `tools/tracker_submit.py submit` first and update the pack row only after it prints `recorded:`. `check-packs` fails while any pack CSV says submitted without an `applied` event.

## Dashboard

`tools/build_report.py` is deterministic (no LLM, no network): same log, same bytes. It writes a self-contained `site/index.html` with stat cards, inline-SVG charts, KPIs (rates are suppressed under 5 applications) and a filterable table. Serve the folder anywhere. This repo publishes a demo from the sample data to GitHub Pages; `app/` deploys it to Cloudflare behind Access.
