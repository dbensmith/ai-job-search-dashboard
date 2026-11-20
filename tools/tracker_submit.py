#!/usr/bin/env python3
"""Record a submitted application in the events log, and check that every submission is recorded.

`submit` is the only supported way to mark a role as applied: it appends dated `applied` events to
`tracker/events.csv` (TRACKER_EVENTS / --events) and regenerates `job_search_tracker.csv`, so a status can no
longer change in a pack's CSV alone. `check-packs` fails when a pack CSV says a role was submitted but the log
has no `applied` event for it. No network, no credentials: the log lives in git.

Usage:
  python3 tools/tracker_submit.py submit <id> [--date YYYY-MM-DD] [--note TEXT] [--artifact PATH] [--commit SHA]
  python3 tools/tracker_submit.py check-packs <packs-dir>

`--artifact` is the repo path of the evidence (the submitted PDF, or the submission record). `--commit` is the
commit that holds it; it defaults to the current HEAD, so commit the artifact first.

Exit codes: 0 ok, 1 check failed, 2 bad input.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracker as t  # noqa: E402
from job_key import make_key  # noqa: E402

# Pack CSV statuses that mean the application has left our hands (see tracker/schema.json for the lifecycle).
SUBMITTED = {"applied", "interview", "offer", "hired", "rejected", "no_response", "no response", "withdrawn",
             "offer_declined", "offer declined"}


def applied_on(role: dict):
    return next((a["date"] for a in role.get("actions", []) if a["type"] == "applied"), None)


def head_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              check=True, cwd=t.ROOT).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def submit(events: Path, csv_path: Path, rid: str, on: str, note: str = "", artifact: str = "", commit: str = "") -> dict:
    roles = t.load_roles(events)
    rid = t.find_id(roles, rid)
    role = next(r for r in roles if r["id"] == rid)
    if applied_on(role):
        return role  # idempotent: re-running after a partial failure records nothing twice
    if role["status"] == "found":  # the lifecycle has no found -> applied; a never-drafted referral passes through considering
        roles = t.append_event(events, rid, "decided_to_apply", on)
    roles = t.append_event(events, rid, "applied", on, note=note, artifact=artifact, commit=commit or head_sha())
    t.sync_csv(roles, csv_path)
    done = next(r for r in roles if r["id"] == rid)
    if not applied_on(done):
        raise SystemExit(f"error: {rid}: the log does not show the application after the write")
    return done


def pack_rows(packs: Path):
    for f in sorted(packs.rglob("*.csv")):
        with open(f, encoding="utf-8", newline="") as fh:
            rd = csv.DictReader(fh)
            if not rd.fieldnames or not {"company", "role", "status"} <= set(rd.fieldnames):
                continue
            for row in rd:
                yield f, row


def check_packs(packs: Path, roles: list) -> list:
    by_id = {r["id"]: r for r in roles}
    problems = []
    for f, row in pack_rows(packs):
        status = (row.get("status") or "").strip().lower()
        if status not in SUBMITTED:
            continue
        rid = make_key(row["company"], row["role"])
        role = by_id.get(rid)
        where = f"{f.relative_to(packs)}"
        if role is None:
            problems.append(f"{where}: {rid}: marked {status} but not in the events log")
        elif not applied_on(role):
            problems.append(f"{where}: {rid}: marked {status} but the log has no applied event (tracker_submit.py submit)")
    return problems


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--events", type=Path, default=t.DEFAULT_EVENTS)
    p.add_argument("--csv", type=Path, default=t.DEFAULT_CSV)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("id")
    s.add_argument("--date", default=date.today().isoformat())
    s.add_argument("--note", default="")
    s.add_argument("--artifact", default="")
    s.add_argument("--commit", default="")
    c = sub.add_parser("check-packs")
    c.add_argument("packs", type=Path)
    a = p.parse_args(argv)
    if a.cmd == "submit":
        if not t.is_date(a.date):
            return _err("--date must be YYYY-MM-DD", 2)
        done = submit(a.events, a.csv, a.id, a.date, a.note, a.artifact, a.commit)
        print(f"recorded: {done['id']} status={done['status']} applied_on={applied_on(done)}")
        print("next: set the pack CSV row to applied (receipt above), then run check-packs")
        return 0
    if not a.packs.is_dir():
        return _err(f"{a.packs} is not a directory", 2)
    problems = check_packs(a.packs, t.load_roles(a.events))
    print("\n".join(problems) if problems else "ok: every submitted pack row is in the events log")
    return 1 if problems else 0


def _err(msg: str, code: int) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
