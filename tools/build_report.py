#!/usr/bin/env python3
"""Build the static site: upstream's report (tools/html_report.py), unchanged, from our events log.

Upstream renders the page from <root>/job_search_tracker.csv and <root>/documents/applications/. This glue only derives
that CSV from the events log (tracker/events.csv) in a temporary folder and writes the page to <out>/index.html.
Without an events log, <root>/job_search_tracker.csv is rendered as it is. No LLM, no network.

  python3 tools/build_report.py [--root DIR] [--events FILE] [--out site]
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import html_report  # noqa: E402
import report_data  # noqa: E402
import tracker as t  # noqa: E402


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=t.ROOT, help="folder holding job_search_tracker.csv and documents/applications/")
    p.add_argument("--events", type=Path, help="events log (default <root>/tracker/events.csv when it exists)")
    p.add_argument("--out", type=Path, default=t.ROOT / "site")
    args = p.parse_args(argv)

    events = args.events or (args.root / "tracker" / "events.csv")
    root, tmp = args.root, None
    if events.exists():
        rows = t.read_events(events)
        errs = t.validate(rows)
        if errs:
            print("\n".join(errs), file=sys.stderr)
            return 1
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        t.sync_csv(t.fold(rows)[0], root / "job_search_tracker.csv")
        apps = args.root / "documents" / "applications"
        if apps.is_dir():
            (root / "documents").mkdir()
            (root / "documents" / "applications").symlink_to(apps.resolve(), target_is_directory=True)
    elif not (root / "job_search_tracker.csv").exists():
        print(f"error: neither {events} nor {root / 'job_search_tracker.csv'} exists", file=sys.stderr)
        return 2
    try:
        page = html_report.render(report_data.load(str(root)))
    finally:
        if tmp:
            tmp.cleanup()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    print(f"ok: {args.out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
