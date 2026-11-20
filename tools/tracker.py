#!/usr/bin/env python3
"""Job search tracker: an append-only events log is the source of truth.

`tracker/events.csv` (override with TRACKER_EVENTS or --events) holds one dated row per action. Roles, their
status and history are *derived* by folding the log, and `job_search_tracker.csv` (the file the upstream
commands read) is regenerated from it after every write. Fictional samples live in `tracker/examples/`.
Nothing edits the log by hand: `add`, `event` and `set` validate the whole log before appending, and
`validate` re-checks it (the unit tests run it). Git history is the backstop; rows are never rewritten.

Columns: date,id,type,company,role,field,value,note,artifact,commit
  found              company+role required (first row for an id)
  set                field+value: one of the editable role fields (latest wins)
  skipped            value = skip reason
  interview          value = stage (optional)
  validated          employer-page check (also stamps validated_on)
  artifact           repo path of the evidence (PDF, posting snapshot, record); commit = commit that holds it

Lifecycle (status):
  found -> considering -> drafted -> applied -> interview -> offer -> hired
  any non-final status -> skipped (needs a reason)
  applied/interview/offer -> rejected | no_response | withdrawn;  offer -> hired | offer_declined
Rule: a role may only leave `found` after it was checked against the employer's own career page
(`validated` event + url; a referral needs only the validated event).

Usage:
  python3 tools/tracker.py validate
  python3 tools/tracker.py add --company X --role Y [--url U --fit 70 ...] [--date D]
  python3 tools/tracker.py event <id> <type> [--date D] [--value V] [--note N] [--artifact PATH] [--commit SHA]
  python3 tools/tracker.py set <id> field=value ...
  python3 tools/tracker.py list [--status S ...] [--open]
  python3 tools/tracker.py export-json | export-csv | export-md     # derived views on stdout
  python3 tools/tracker.py sync-csv                                  # rewrite job_search_tracker.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_key import make_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = json.loads((ROOT / "tracker" / "schema.json").read_text(encoding="utf-8"))
SCHEMA_VERSION = _SCHEMA["schema_version"]
STATUSES = tuple(_SCHEMA["statuses"])
FINAL = set(_SCHEMA["final"])
TRANSITIONS = {k: set(v) for k, v in _SCHEMA["transitions"].items()}
STAGES = tuple(_SCHEMA["stages"])
SKIP_REASONS = tuple(_SCHEMA["skip_reasons"])
EVENT_TYPES = tuple(_SCHEMA["event_types"])
EVENT_STATUS = dict(_SCHEMA["event_status"])
EVENT_COLUMNS = list(_SCHEMA["event_columns"])
CATEGORIES = tuple(_SCHEMA["categories"])
SENIORITY = tuple(_SCHEMA["seniority"])
CHANNELS = tuple(_SCHEMA["channels"])
CURRENCIES = tuple(_SCHEMA["currencies"])
PRIORITIES = tuple(_SCHEMA["priorities"])
LOCATION_MODES = tuple(_SCHEMA["location_modes"])
WORK_AUTH = tuple(_SCHEMA["work_auth"])
NEEDS_VALIDATION = set(STATUSES) - {"found", "skipped"}
EDITABLE = set(_SCHEMA["editable"])
INT_FIELDS = set(_SCHEMA["int_fields"])
ENUM_FIELDS = {"priority": PRIORITIES, "location_mode": LOCATION_MODES, "work_auth": WORK_AUTH,
               "skip_reason": SKIP_REASONS, "category": CATEGORIES, "seniority": SENIORITY,
               "channel": CHANNELS, "comp_currency": CURRENCIES}
DATE_FIELDS = ("validated_on", "deadline", "posted_on", "closed_on")

DEFAULT_EVENTS = Path(os.environ.get("TRACKER_EVENTS") or ROOT / "tracker" / "events.csv")
DEFAULT_CSV = Path(os.environ.get("TRACKER_CSV") or ROOT / "job_search_tracker.csv")

# Upstream's job_search_tracker.csv header (see .claude/commands/outcome.md).
LEGACY_COLS = ["date", "company", "sector", "role", "role_type", "channel", "status",
               "contact_person", "fit_rating", "notes", "cv_file", "cover_letter_file",
               "source", "deadline"]
LEGACY_CHANNEL = {"linkedin": "portal", "employer_site": "online", "referral": "referral"}


def today() -> str:
    return date.today().isoformat()


def is_date(value) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- log I/O

def read_events(path: Path = DEFAULT_EVENTS) -> list:
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        if rd.fieldnames != EVENT_COLUMNS:
            raise SystemExit(f"error: {path}: header must be {','.join(EVENT_COLUMNS)}")
        rows = []
        for n, row in enumerate(rd, start=2):
            if None in row or None in row.values():
                raise SystemExit(f"error: {path}:{n}: wrong number of fields")
            row["_line"] = n
            rows.append(row)
        return rows


def write_events(rows: list, path: Path = DEFAULT_EVENTS) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=EVENT_COLUMNS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- folding

def fold(rows: list) -> tuple:
    """Fold the event log into role dicts. Returns (roles, errors); roles are sorted by id."""
    roles: dict = {}
    errs: list = []
    for row in rows:
        where = f"line {row.get('_line', '?')}"
        rid, typ, on = row["id"].strip(), row["type"].strip(), row["date"].strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", rid):
            errs.append(f"{where}: id {rid!r} must be lowercase letters, digits, _ or - (max 128)")
            continue
        if typ not in EVENT_TYPES:
            errs.append(f"{where}: {rid}: unknown event type {typ!r}")
            continue
        if not is_date(on):
            errs.append(f"{where}: {rid}: date must be YYYY-MM-DD")
            continue
        r = roles.get(rid)
        if typ == "found":
            if r is not None:
                errs.append(f"{where}: {rid}: duplicate found")
                continue
            if not (row["company"].strip() and row["role"].strip()):
                errs.append(f"{where}: {rid}: found requires company and role")
                continue
            r = roles[rid] = {"id": rid, "company": row["company"].strip(), "role": row["role"].strip(),
                              "status": "found", "found_on": on, "history": [{"date": on, "status": "found"}],
                              "actions": [], "events": []}
        elif r is None:
            errs.append(f"{where}: {rid}: {typ} before found")
            continue
        if r["events"] and on < r["events"][-1]["date"]:
            errs.append(f"{where}: {rid}: date {on} is earlier than the previous event for this role")
        ev = {k: row[k].strip() for k in ("type", "value", "note", "artifact", "commit")}
        ev["date"] = on
        r["events"].append(ev)
        if typ == "set":
            f, v = row["field"].strip(), row["value"].strip()
            if f not in EDITABLE:
                errs.append(f"{where}: {rid}: {f!r} is not an editable field")
            elif v == "":
                r.pop(f, None)
            else:
                r[f] = int(v) if f in INT_FIELDS and re.fullmatch(r"-?\d+", v) else (v == "true" if f == "referral" else v)
            continue
        if typ == "validated":
            r["validated_on"] = on
        if typ == "role_closed":
            r["closed_on"] = on
        if typ not in ("found", "set"):
            r["actions"].append({"date": on, "type": typ, **({"note": ev["note"]} if ev["note"] else {})})
        status = EVENT_STATUS.get(typ)
        if status and typ != "found":
            cur = r["status"]
            allowed = TRANSITIONS.get(cur, set()) | ({"skipped"} if cur not in FINAL else set())
            if status not in allowed:
                errs.append(f"{where}: {rid}: {typ} not allowed while {cur}")
                continue
            entry = {"date": on, "status": status}
            if ev["note"]:
                entry["note"] = ev["note"]
            r["status"] = status
            r.pop("stage", None)
            if status == "interview" and ev["value"]:
                r["stage"] = ev["value"]
                entry["stage"] = ev["value"]
            if status == "skipped":
                r["skip_reason"] = ev["value"]
            r["history"].append(entry)
    return [roles[k] for k in sorted(roles)], errs


def validate_role(r: dict) -> list:
    rid = r["id"]
    errs = []
    st = r["status"]
    for k, allowed in ENUM_FIELDS.items():
        if r.get(k) is not None and r[k] not in allowed:
            errs.append(f"{rid}: {k} {r[k]!r} not in {allowed}")
    fit = r.get("fit")
    if fit is not None and not (isinstance(fit, int) and 0 <= fit <= 100):
        errs.append(f"{rid}: fit must be an integer 0-100")
    for k in INT_FIELDS - {"fit"}:
        if r.get(k) is not None and not isinstance(r[k], int):
            errs.append(f"{rid}: {k} must be an integer (annual base)")
    if r.get("comp_min") is not None and r.get("comp_max") is not None and r["comp_min"] > r["comp_max"]:
        errs.append(f"{rid}: comp_min exceeds comp_max")
    if (r.get("comp_min") or r.get("comp_max")) and not r.get("comp_currency"):
        errs.append(f"{rid}: comp_currency required with numeric comp")
    for k in DATE_FIELDS:
        if r.get(k) is not None and not is_date(r[k]):
            errs.append(f"{rid}: {k} must be YYYY-MM-DD")
    if r.get("stage") is not None and r["stage"] not in STAGES:
        errs.append(f"{rid}: stage {r['stage']!r} not in {STAGES}")
    if st == "skipped" and r.get("skip_reason") not in SKIP_REASONS:
        errs.append(f"{rid}: skipped requires a skip reason from {SKIP_REASONS}")
    if st in NEEDS_VALIDATION and not (r.get("validated_on") and (r.get("url") or r.get("referral") is True)):
        errs.append(f"{rid}: {st} requires a validated event and url (employer-page check; referral roles need only validated)")
    return errs


def validate(rows: list) -> list:
    roles, errs = fold(rows)
    for r in roles:
        errs += validate_role(r)
    return errs


def load_roles(path: Path = DEFAULT_EVENTS) -> list:
    roles, errs = fold(read_events(path))
    if errs:
        raise SystemExit("error: invalid events log\n" + "\n".join(errs))
    return roles


# ---------------------------------------------------------------- derived views

def is_open(r: dict) -> bool:
    return r["status"] not in FINAL


def legacy_rows(roles: list) -> list:
    """Rows for upstream's job_search_tracker.csv (roles that reached drafted or later)."""
    out = []
    for r in roles:
        if r["status"] in ("found", "considering", "skipped"):
            continue
        first = next((h["date"] for h in r["history"] if h["status"] in ("drafted", "applied")), r["found_on"])
        out.append({"date": first, "company": r["company"], "sector": r.get("sector", ""), "role": r["role"],
                    "role_type": r.get("role_type", ""), "channel": LEGACY_CHANNEL.get(r.get("channel"), "other"),
                    "status": r["status"], "contact_person": r.get("contact", ""),
                    "fit_rating": r.get("fit", ""), "notes": r.get("notes", ""),
                    "cv_file": r.get("cv_file", ""), "cover_letter_file": r.get("cover_letter_file", ""),
                    "source": r.get("url", ""), "deadline": r.get("deadline", "")})
    return out


def legacy_csv(roles: list) -> str:
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=LEGACY_COLS, lineterminator="\n")
    w.writeheader()
    w.writerows(legacy_rows(roles))
    return buf.getvalue()


def sync_csv(roles: list, path: Path = DEFAULT_CSV) -> None:
    Path(path).write_text(legacy_csv(roles), encoding="utf-8", newline="")


def export_md(roles: list) -> str:
    order = ["interview", "offer", "applied", "drafted", "considering", "found"]
    out = ["# Role tracker status", "", "Generated by `python3 tools/tracker.py export-md`. Do not edit by hand.", "",
           f"{len(roles)} roles, {sum(is_open(r) for r in roles)} open.", "", "| Status | Count |", "|---|---|"]
    for st in order + ["skipped"]:
        out.append(f"| {st} | {sum(r['status'] == st for r in roles)} |")
    op = sorted((r for r in roles if is_open(r) and r["status"] in order),
                key=lambda r: (order.index(r["status"]), -(r.get("fit") or 0)))
    out += ["", "## Active roles", "", "| Role | Status | Fit | Notes |", "|---|---|---|---|"]
    for r in op:
        link = f"[{r['role']}]({r['url']})" if r.get("url") else r["role"]
        out.append(f"| {r['company']}: {link} | {r['status']} | {r.get('fit') or '-'} | {r.get('notes', '')} |")
    out += ["", "## Skipped", "", "| Role | Reason |", "|---|---|"]
    out += [f"| {r['company']}: {r['role']} | {r.get('skip_reason')} |" for r in roles if r["status"] == "skipped"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- appending

def append_event(path: Path, rid: str, typ: str, on: str, *, company="", role="", field="", value="",
                 note="", artifact="", commit="") -> list:
    """Validate then append one row; returns the folded roles. Raises SystemExit on any error."""
    rows = read_events(path)
    rows.append({"date": on, "id": rid, "type": typ, "company": company, "role": role, "field": field,
                 "value": value, "note": note, "artifact": artifact, "commit": commit, "_line": len(rows) + 2})
    errs = validate(rows)
    if errs:
        raise SystemExit("error: " + "\n".join(errs))
    write_events(rows, path)
    return fold(rows)[0]


def find_id(roles: list, rid: str) -> str:
    hits = [r["id"] for r in roles if r["id"] == rid] or [r["id"] for r in roles if r["id"].startswith(rid)]
    if len(hits) != 1:
        raise SystemExit(f"error: {len(hits)} roles match {rid!r}")
    return hits[0]


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    a = sub.add_parser("add")
    a.add_argument("--company", required=True)
    a.add_argument("--role", required=True)
    a.add_argument("--date")
    for f in ("url", "source", "region", "comp", "notes", "flags"):
        a.add_argument(f"--{f}")
    a.add_argument("--fit", type=int)
    a.add_argument("--priority", choices=PRIORITIES)
    a.add_argument("--location-mode", choices=LOCATION_MODES)
    a.add_argument("--work-auth", choices=WORK_AUTH)
    e = sub.add_parser("event")
    e.add_argument("id")
    e.add_argument("type", choices=[t for t in EVENT_TYPES if t not in ("found", "set")])
    e.add_argument("--date")
    e.add_argument("--value", default="")
    e.add_argument("--note", default="")
    e.add_argument("--artifact", default="")
    e.add_argument("--commit", default="")
    s = sub.add_parser("set")
    s.add_argument("id")
    s.add_argument("pairs", nargs="+")
    s.add_argument("--date")
    ls = sub.add_parser("list")
    ls.add_argument("--status", nargs="*")
    ls.add_argument("--open", action="store_true")
    for name in ("export-json", "export-csv", "export-md", "sync-csv"):
        sub.add_parser(name)
    args = p.parse_args(argv)

    rows = read_events(args.events)
    roles, errs = fold(rows)
    if args.cmd == "validate":
        errs = validate(rows)
        print("\n".join(errs) if errs else f"ok: {len(roles)} roles, {len(rows)} events")
        return 1 if errs else 0
    if errs and args.cmd not in ("add", "event", "set"):
        print("\n".join(errs), file=sys.stderr)
        return 1
    if args.cmd == "list":
        for r in roles:
            if (args.open and not is_open(r)) or (args.status and r["status"] not in args.status):
                continue
            print(f"{r['status']:<12} {str(r.get('fit') or '-'):>3}  {r['company']} | {r['role']}  [{r['id']}]")
        return 0
    if args.cmd == "export-json":
        json.dump({"schema_version": SCHEMA_VERSION, "roles": roles}, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
        print()
        return 0
    if args.cmd == "export-csv":
        sys.stdout.write(legacy_csv(roles))
        return 0
    if args.cmd == "export-md":
        sys.stdout.write(export_md(roles))
        return 0
    if args.cmd == "sync-csv":
        sync_csv(roles, args.csv)
        print(f"ok: {args.csv}")
        return 0

    on = getattr(args, "date", None) or today()
    if args.cmd == "add":
        rid = make_key(args.company, args.role)
        if any(r["id"] == rid for r in roles):
            raise SystemExit(f"error: {rid} already tracked")
        roles = append_event(args.events, rid, "found", on, company=args.company, role=args.role)
        fields = {"url": args.url, "source": args.source, "region": args.region, "comp": args.comp,
                  "notes": args.notes, "flags": args.flags, "fit": args.fit, "priority": args.priority,
                  "location_mode": args.location_mode, "work_auth": args.work_auth}
        for k, v in fields.items():
            if v is not None:
                roles = append_event(args.events, rid, "set", on, field=k, value=str(v))
    elif args.cmd == "event":
        rid = find_id(roles, args.id)
        roles = append_event(args.events, rid, args.type, on, value=args.value, note=args.note,
                             artifact=args.artifact, commit=args.commit)
    else:
        rid = find_id(roles, args.id)
        for pair in args.pairs:
            k, _, v = pair.partition("=")
            roles = append_event(args.events, rid, "set", on, field=k, value=v)
    sync_csv(roles, args.csv)
    print(f"ok: {rid} -> {next(r['status'] for r in roles if r['id'] == rid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
