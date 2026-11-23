#!/usr/bin/env python3
"""Build the static job search dashboard: one self-contained HTML page, no LLM, no network, deterministic.

Reads the append-only events log (tracker/events.csv; see tools/tracker.py) or, when there is no log, the
upstream-format job_search_tracker.csv, plus optional documents/applications/*/outcome.md for interview
stages. Writes <out>/index.html with stat cards, inline-SVG charts (status, sector, channel, funnel), KPIs
(response rate, days to reply, pay vs floor) and a filterable table with links to the evidence in the repo.
The output folder is provider-neutral: GitHub Pages, Cloudflare Worker assets or any web server can serve it.

Usage:
  python3 tools/build_report.py [--events FILE] [--csv FILE] [--applications DIR] [--out site]
                                [--floor 175000 --currency CAD] [--repo-url URL] [--title TEXT] [--generated DATE]

Same inputs, same bytes out: the "generated" date defaults to the newest event date, not the clock.
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import sys
from datetime import date
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracker as t  # noqa: E402

BUCKETS = ["Drafted", "Active", "Interview", "Offer", "Rejected/Closed"]
BUCKET_OF = {"drafted": "Drafted", "applied": "Active", "interview": "Interview", "offer": "Offer", "hired": "Offer",
             "rejected": "Rejected/Closed", "no_response": "Rejected/Closed", "no response": "Rejected/Closed",
             "offer_declined": "Rejected/Closed", "offer declined": "Rejected/Closed", "withdrawn": "Rejected/Closed"}
RESPONSE_EVENTS = {"response_received", "screen_scheduled", "interview", "offer", "hired", "rejected"}
MIN_RATE_N = 5  # a rate over fewer applications is noise, so it is suppressed
PALETTE = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"]
STAGE_REACH = {"interview": 2, "offer": 3, "hired": 4}


def bucket_of(status: str):
    return BUCKET_OF.get((status or "").strip().lower())


# ------------------------------------------------------------------ data

def records_from_events(roles: list) -> list:
    out = []
    for r in roles:
        applied = next((a["date"] for a in r["actions"] if a["type"] == "applied"), None)
        # A reply before the application (referral outreach) is not a reply to the application.
        resp = next((a["date"] for a in r["actions"]
                     if a["type"] in RESPONSE_EVENTS and not (applied and a["date"] < applied)), None)
        reached = 1 if applied else 0
        if resp:
            reached = max(reached, 1)
        for h in r["history"]:
            reached = max(reached, STAGE_REACH.get(h["status"], 0))
        out.append({"id": r["id"], "company": r["company"], "role": r["role"], "status": r["status"],
                    "sector": r.get("sector", ""), "channel": r.get("channel", ""), "fit": r.get("fit"),
                    "date": applied or r["found_on"], "applied_on": applied, "responded_on": resp,
                    "reached": reached, "url": r.get("url", ""), "comp_max": r.get("comp_max"),
                    "currency": r.get("comp_currency", ""), "notes": r.get("notes", ""),
                    "skip_reason": r.get("skip_reason", ""), "stage": r.get("stage", ""),
                    "artifacts": [(e["artifact"], e["commit"]) for e in r["events"] if e["artifact"]],
                    "dates": [e["date"] for e in r["events"]]})
    return out


def records_from_csv(path: Path) -> list:
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            row = {k: (v or "") for k, v in raw.items() if k}  # short rows leave None values
            st = row.get("status", "").strip().lower().replace(" ", "_")
            fit = row.get("fit_rating", "").strip()
            out.append({"id": t.make_key(row.get("company", ""), row.get("role", "")), "company": row.get("company", ""),
                        "role": row.get("role", ""), "status": st, "sector": row.get("sector", ""),
                        "channel": row.get("channel", ""), "fit": int(fit) if fit.isdigit() else None,
                        "date": row.get("date", ""), "applied_on": None, "responded_on": None,
                        "reached": 0 if st == "drafted" else max(1, STAGE_REACH.get(st, 0)),
                        "url": row.get("source", ""), "comp_max": None, "currency": "", "notes": row.get("notes", ""),
                        "skip_reason": "", "stage": "", "artifacts": [], "dates": [row.get("date", "")]})
    return out


def merge_outcomes(records: list, apps: Path) -> None:
    """A ticked interview-stage checkbox in outcome.md proves the stage was reached even when the status moved on."""
    if not apps or not apps.is_dir():
        return
    for f in sorted(apps.glob("*/outcome.md")):
        key = re.sub(r"[^a-z0-9]", "", f.parent.name.lower())
        ticked = len(re.findall(r"^\s*[-*] \[[xX]\]", f.read_text(encoding="utf-8", errors="replace"), re.M))
        for r in records:
            if re.sub(r"[^a-z0-9]", "", f"{r['company']}{r['role']}".lower()) == key and ticked:
                r["reached"] = max(r["reached"], 2)


def stats(records: list, floor, currency) -> dict:
    sent = [r for r in records if r["reached"] >= 1 and bucket_of(r["status"]) and r["status"] != "drafted"]
    counts = {b: sum(bucket_of(r["status"]) == b for r in records) for b in BUCKETS}
    sent_n = len(sent)
    responded = [r for r in sent if r["responded_on"] or r["reached"] >= 2 or r["status"] == "rejected"]
    days = []
    for r in sent:
        if r["applied_on"] and r["responded_on"]:
            days.append((date.fromisoformat(r["responded_on"]) - date.fromisoformat(r["applied_on"])).days)
    resolved = [r for r in sent if r["status"] in ("rejected", "no_response")]
    final = [r for r in sent if r["status"] in t.FINAL]
    by = lambda k: sorted(((v or "unspecified", n) for v, n in _count(r[k] for r in sent).items()), key=lambda x: (-x[1], x[0]))
    funnel = [("Applied", sent_n)] + [(name, sum(r["reached"] >= lvl for r in sent)) for name, lvl in
                                      (("Interview", 2), ("Offer", 3), ("Hired", 4))]
    priced = [r for r in records if r["comp_max"] and floor and r["currency"] == currency]
    return {"counts": counts, "sent": sent_n, "drafted": counts["Drafted"], "by_sector": by("sector"),
            "by_channel": by("channel"), "funnel": funnel,
            "response_rate": len(responded) / sent_n if sent_n >= MIN_RATE_N else None,
            "median_days": statistics.median(days) if len(days) >= MIN_RATE_N else None, "days_n": len(days),
            "rejection_rate": len(resolved) / len(final) if len(final) >= MIN_RATE_N else None,
            "priced": len(priced), "above_floor": sum(r["comp_max"] >= floor for r in priced) if floor else 0}


def _count(it) -> dict:
    d: dict = {}
    for v in it:
        d[v] = d.get(v, 0) + 1
    return d


# ------------------------------------------------------------------ svg

def donut(items: list) -> str:
    total = sum(n for _, n in items)
    if not total:
        return '<p class="muted">No applications yet.</p>'
    cx = cy = 80
    r, w = 62, 26
    ang = -math.pi / 2
    paths = []
    for i, (label, n) in enumerate(items):
        if not n:
            continue
        sweep = 2 * math.pi * n / total
        if n == total:  # a full circle cannot be one arc
            paths.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="var({PALETTE[i % 6]})" stroke-width="{w}"/>')
            break
        x1, y1 = cx + r * math.cos(ang), cy + r * math.sin(ang)
        ang += sweep
        x2, y2 = cx + r * math.cos(ang), cy + r * math.sin(ang)
        paths.append(f'<path d="M{x1:.2f} {y1:.2f} A{r} {r} 0 {int(sweep > math.pi)} 1 {x2:.2f} {y2:.2f}" fill="none" '
                     f'stroke="var({PALETTE[i % 6]})" stroke-width="{w}"><title>{escape(label)}: {n}</title></path>')
    legend = "".join(f'<li><i style="background:var({PALETTE[i % 6]})"></i>{escape(l)} <b>{n}</b></li>'
                     for i, (l, n) in enumerate(items) if n)
    return (f'<div class="donut"><svg viewBox="0 0 160 160" role="img" aria-label="Status breakdown">{"".join(paths)}'
            f'<text x="80" y="86" text-anchor="middle" class="big">{total}</text></svg><ul class="legend">{legend}</ul></div>')


def bars(items: list, label: str) -> str:
    if not items:
        return '<p class="muted">No data.</p>'
    top = max(n for _, n in items) or 1
    rows = "".join(
        f'<div class="bar"><span class="bl" title="{escape(l)}">{escape(l)}</span>'
        f'<span class="bt"><span class="bf" style="width:{100 * n / top:.1f}%"></span></span><b>{n}</b></div>'
        for l, n in items[:10])
    return f'<div class="bars" role="img" aria-label="{escape(label)}">{rows}</div>'


# ------------------------------------------------------------------ page

CSS = """
:root{--bg:#f7f7f5;--fg:#1c1c1a;--muted:#6b6b66;--card:#fff;--line:#e2e2dc;--accent:#2b6cb0;
--c1:#2b6cb0;--c2:#2f855a;--c3:#b7791f;--c4:#c05621;--c5:#6b46c1;--c6:#718096}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141413;--fg:#ececea;--muted:#9a9a94;--card:#1d1d1b;
--line:#33332f;--accent:#63b3ed;--c1:#63b3ed;--c2:#68d391;--c3:#f6ad55;--c4:#fc8181;--c5:#b794f4;--c6:#a0aec0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:1.4rem;margin:.2em 0}h2{font-size:1rem;margin:0 0 .6em}
.muted{color:var(--muted)}.cards,.grid{display:grid;gap:12px;margin:12px 0}.cards{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}
.grid{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.card .n{font-size:1.8rem;font-weight:650}
.donut{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.donut svg{width:150px}.big{font-size:30px;font-weight:650;fill:var(--fg)}
.legend{list-style:none;margin:0;padding:0}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.bar{display:flex;align-items:center;gap:8px;margin:4px 0}.bl{width:34%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bt{flex:1;background:var(--line);border-radius:4px;height:12px}.bf{display:block;height:12px;border-radius:4px;background:var(--accent)}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);
border-radius:6px;background:var(--card);color:var(--fg)}.tw{overflow-x:auto}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:.8rem;color:var(--muted)}
a{color:var(--accent)}code{font-size:.85em}
"""

JS = """
(function(){var q=document.getElementById('q'),s=document.getElementById('st'),rows=[].slice.call(document.querySelectorAll('tbody tr'));
function f(){var t=q.value.toLowerCase();rows.forEach(function(r){r.hidden=!((!s.value||r.dataset.status===s.value)&&(!t||r.textContent.toLowerCase().indexOf(t)>=0))})}
q.addEventListener('input',f);s.addEventListener('change',f)})();
"""


def pct(v):
    return f"{100 * v:.0f}%" if v is not None else f"n&lt;{MIN_RATE_N}"


def artifact_link(path: str, commit: str, repo_url: str) -> str:
    label = escape(path.rsplit("/", 1)[-1] or path)
    if repo_url and commit and re.fullmatch(r"[0-9a-f]{7,40}", commit) and not path.startswith(("/", "..")):
        return f'<a href="{escape(repo_url.rstrip("/"))}/blob/{commit}/{escape(path)}" rel="noopener">{label}</a>'
    return f"<code>{escape(path)}</code>"


def render(records: list, st: dict, args, generated: str) -> str:
    cards = [("Sent", st["sent"])] + [(b, st["counts"][b]) for b in BUCKETS]
    cards_html = "".join(f'<div class="card"><div class="muted">{escape(k)}</div><div class="n">{v}</div></div>' for k, v in cards)
    donut_items = [(b, st["counts"][b]) for b in BUCKETS]
    funnel = [(k, v) for k, v in st["funnel"]]
    kpis = [("Response rate", pct(st["response_rate"])),
            ("Median days to reply", f"{st['median_days']:.0f}" if st["median_days"] is not None else f"n&lt;{MIN_RATE_N}"),
            ("Rejection rate", pct(st["rejection_rate"]))]
    if args.floor:
        kpis.append((f"Top of range at or above {args.floor:,} {escape(args.currency)}",
                     f"{st['above_floor']} of {st['priced']}"))
    kpi_html = "".join(f'<div class="card"><div class="muted">{k}</div><div class="n">{v}</div></div>' for k, v in kpis)
    order = {"interview": 0, "offer": 1, "applied": 2, "drafted": 3, "considering": 4, "found": 5}
    shown = sorted(records, key=lambda r: (order.get(r["status"], 9), -(r["fit"] or 0), r["company"].lower()))
    statuses = sorted({r["status"] for r in records})
    body = []
    for r in shown:
        links = " ".join(artifact_link(p, c, args.repo_url) for p, c in r["artifacts"])
        name = f'<a href="{escape(r["url"])}" rel="noopener">{escape(r["role"])}</a>' if r["url"].startswith("https://") else escape(r["role"])
        status = r["status"] + (f" ({r['stage']})" if r["stage"] else "") + (f": {r['skip_reason']}" if r["skip_reason"] else "")
        body.append(f'<tr data-status="{escape(r["status"])}"><td>{escape(r["date"])}</td><td>{escape(r["company"])}</td>'
                    f'<td>{name}</td><td>{escape(status)}</td><td>{r["fit"] if r["fit"] is not None else ""}</td>'
                    f'<td>{links}</td></tr>')
    options = '<option value="">All statuses</option>' + "".join(f'<option>{escape(s)}</option>' for s in statuses)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(args.title)}</title><style>{CSS}</style></head><body><main>
<h1>{escape(args.title)}</h1><p class="muted">Generated {escape(generated)} from the events log. {len(records)} roles tracked.</p>
<div class="cards">{cards_html}</div>
<div class="grid"><div class="card"><h2>Status</h2>{donut(donut_items)}</div>
<div class="card"><h2>Funnel</h2>{bars(funnel, "Funnel")}</div>
<div class="card"><h2>By sector</h2>{bars(st["by_sector"], "Applications by sector")}</div>
<div class="card"><h2>By channel</h2>{bars(st["by_channel"], "Applications by channel")}</div></div>
<div class="cards">{kpi_html}</div>
<div class="card"><h2>Roles</h2><div class="filters"><select id="st" aria-label="Status">{options}</select>
<input id="q" type="search" placeholder="Search" aria-label="Search"></div>
<div class="tw"><table><thead><tr><th>Date</th><th>Company</th><th>Role</th><th>Status</th><th>Fit</th><th>Evidence</th></tr></thead>
<tbody>{"".join(body)}</tbody></table></div></div>
<p class="muted">Rates need at least {MIN_RATE_N} applications. Drafted roles are excluded from every rate.</p>
</main><script>{JS}</script></body></html>
"""


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--events", type=Path, default=t.DEFAULT_EVENTS)
    p.add_argument("--csv", type=Path, default=t.DEFAULT_CSV)
    p.add_argument("--applications", type=Path, default=t.ROOT / "documents" / "applications")
    p.add_argument("--out", type=Path, default=t.ROOT / "site")
    p.add_argument("--floor", type=int)
    p.add_argument("--currency", default="CAD")
    p.add_argument("--repo-url", default="")
    p.add_argument("--title", default="Job search tracker dashboard")
    p.add_argument("--generated")
    args = p.parse_args(argv)

    if args.events.exists():
        rows = t.read_events(args.events)
        errs = t.validate(rows)
        if errs:
            print("\n".join(errs), file=sys.stderr)
            return 1
        records = records_from_events(t.fold(rows)[0])
    elif args.csv.exists():
        records = records_from_csv(args.csv)
    else:
        print(f"error: neither {args.events} nor {args.csv} exists", file=sys.stderr)
        return 2
    merge_outcomes(records, args.applications)
    seen = [d for r in records for d in r["dates"] if t.is_date(d)]
    generated = args.generated or (max(seen) if seen else "no data")
    page = render(records, stats(records, args.floor, args.currency), args, generated)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    print(f"ok: {args.out / 'index.html'} ({len(records)} roles)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
