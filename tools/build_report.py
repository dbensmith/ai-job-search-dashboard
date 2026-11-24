#!/usr/bin/env python3
"""Build the static dashboard: upstream's report (tools/html_report.py) plus a small add-on for what the events log knows.

Upstream renders the page from job_search_tracker.csv and documents/applications/. This wrapper only supplies those
inputs and adds one panel that the CSV cannot carry: response rate, median days to reply, top of pay range against a
floor, and links to the evidence in the repo (path plus commit). No LLM, no network.

  python3 tools/build_report.py [--root DIR] [--events FILE] [--out site] [--floor 175000 --currency CAD] [--repo-url URL]

With --events (default <root>/tracker/events.csv, when it exists) the CSV is derived from the log in a temporary
folder and the add-on is written. Without a log, <root>/job_search_tracker.csv is rendered as upstream does, with no
add-on. Output is <out>/index.html. Like upstream's, the page shows today's date and days waiting, so a rebuild on
another day differs; the same day gives the same bytes.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
from datetime import date
from html import escape
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parent))
import html_report  # noqa: E402
import report_data  # noqa: E402
import tracker as t  # noqa: E402

RESPONSE_EVENTS = {"response_received", "screen_scheduled", "interview", "offer", "hired", "rejected"}
MIN_RATE_N = 5  # a rate over fewer applications is noise, so it is suppressed


def records_from_events(roles: list) -> list:
    out = []
    for r in roles:
        applied = next((a["date"] for a in r["actions"] if a["type"] == "applied"), None)
        # A reply before the application (referral outreach) is not a reply to the application.
        resp = next((a["date"] for a in r["actions"]
                     if a["type"] in RESPONSE_EVENTS and not (applied and a["date"] < applied)), None)
        out.append({"company": r["company"], "role": r["role"], "status": r["status"], "applied_on": applied,
                    "responded_on": resp, "comp_max": r.get("comp_max"), "currency": r.get("comp_currency", ""),
                    "artifacts": [(e["artifact"], e["commit"]) for e in r["events"] if e["artifact"]]})
    return out


def stats(records: list, floor, currency) -> dict:
    sent = [r for r in records if r["applied_on"]]
    responded = [r for r in sent if r["responded_on"] or r["status"] in ("interview", "offer", "hired", "rejected")]
    days = [(date.fromisoformat(r["responded_on"]) - date.fromisoformat(r["applied_on"])).days
            for r in sent if r["responded_on"]]
    priced = [r for r in records if r["comp_max"] and floor and r["currency"] == currency]
    return {"sent": len(sent),
            "response_rate": len(responded) / len(sent) if len(sent) >= MIN_RATE_N else None,
            "median_days": statistics.median(days) if len(days) >= MIN_RATE_N else None,
            "priced": len(priced), "above_floor": sum(r["comp_max"] >= floor for r in priced) if floor else 0}


def artifact_link(path: str, commit: str, repo_url: str) -> str:
    label = escape(path.rsplit("/", 1)[-1] or path)
    if repo_url and commit and re.fullmatch(r"[0-9a-f]{7,40}", commit) and not path.startswith(("/", "..")):
        return f'<a href="{escape(repo_url.rstrip("/"))}/blob/{commit}/{escape(path)}" rel="noopener">{label}</a>'
    return f"<code>{escape(path)}</code>"


def pct(v):
    return f"{100 * v:.0f}%" if v is not None else f"n&lt;{MIN_RATE_N}"


def addon(records: list, st: dict, floor, currency, repo_url: str) -> str:
    kpis = [("Response rate", pct(st["response_rate"])),
            ("Median days to reply", f"{st['median_days']:.0f}" if st["median_days"] is not None else f"n&lt;{MIN_RATE_N}")]
    if floor:
        kpis.append((f"Top of range at or above {floor:,} {escape(currency)}", f"{st['above_floor']} of {st['priced']}"))
    cards = "".join(f'<div class="panel"><div class="panel-head"><h3 class="h3">{k}</h3></div>'
                    f'<div class="kpi-value num">{v}</div></div>' for k, v in kpis)
    rows = "".join(
        f"<tr><td>{escape(r['company'])}</td><td>{escape(r['role'])}</td><td>"
        + " ".join(artifact_link(p, c, repo_url) for p, c in r["artifacts"]) + "</td></tr>"
        for r in sorted(records, key=lambda r: (r["company"].lower(), r["role"].lower())) if r["artifacts"])
    table = (f'<div class="panel"><div class="panel-head"><h3 class="h3">Evidence</h3><p>Documents in the repo</p></div>'
             f'<div class="table-wrap" style="max-height:none"><table class="data"><thead><tr><th scope="col">Company</th>'
             f'<th scope="col">Role</th><th scope="col">Files</th></tr></thead><tbody>{rows}</tbody></table></div></div>') if rows else ""
    return (f'<section id="events" aria-labelledby="h-events"><h2 id="h-events" class="sr-only">From the events log</h2>'
            f'<div class="grid-3" style="display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(14rem,1fr))">'
            f'{cards}</div>{table}'
            f'<p class="muted">Rates need at least {MIN_RATE_N} applications. Drafted roles are not counted as sent.</p></section>')


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=t.ROOT, help="folder holding job_search_tracker.csv and documents/applications/")
    p.add_argument("--events", type=Path, help="events log (default <root>/tracker/events.csv when it exists)")
    p.add_argument("--out", type=Path, default=t.ROOT / "site")
    p.add_argument("--floor", type=int)
    p.add_argument("--currency", default="CAD")
    p.add_argument("--repo-url", default="")
    args = p.parse_args(argv)

    events = args.events or (args.root / "tracker" / "events.csv")
    extra, root, tmp = "", args.root, None
    if events.exists():
        rows = t.read_events(events)
        errs = t.validate(rows)
        if errs:
            print("\n".join(errs), file=sys.stderr)
            return 1
        roles = t.fold(rows)[0]
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        t.sync_csv(roles, root / "job_search_tracker.csv")
        apps = args.root / "documents" / "applications"
        if apps.is_dir():
            (root / "documents").mkdir()
            (root / "documents" / "applications").symlink_to(apps.resolve(), target_is_directory=True)
        records = records_from_events(roles)
        extra = addon(records, stats(records, args.floor, args.currency), args.floor, args.currency, args.repo_url)
    elif not (root / "job_search_tracker.csv").exists():
        print(f"error: neither {events} nor {root / 'job_search_tracker.csv'} exists", file=sys.stderr)
        return 2
    try:
        page = html_report.render(report_data.load(str(root)))
    finally:
        if tmp:
            tmp.cleanup()
    if extra:
        page = page.replace("</main>", extra + "\n</main>", 1)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    print(f"ok: {args.out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
