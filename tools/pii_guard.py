#!/usr/bin/env python3
"""PII guard: keeps personal data out of this repository.

This repo is meant to be public. It holds the method (code, templates, runbooks),
never a real person's profile. This guard blocks personal data before it lands:

    python tools/pii_guard.py --staged              pre-commit hook: staged changes
    python tools/pii_guard.py --message-file FILE   commit-msg hook: the message
    python tools/pii_guard.py --range A...B         CI: a PR's added lines and messages
    python tools/pii_guard.py --all                 every tracked file
    python tools/pii_guard.py --install-hooks       point git at .githooks/

Two layers:

1. Generic rules (always on): email addresses, phone numbers, credentials,
   32-hex account ids, Cloudflare hostnames, street addresses and postal codes,
   home-directory paths, and paths that only ever hold personal output.
2. A maintainer deny list of the exact things that identify the maintainer
   (name, employers, city, ...). The list is itself personal data, so it is NEVER
   a tracked file. It is read from, in order: the PII_DENYLIST environment
   variable (one case-insensitive regex per line; this is the CI secret), the
   file named by PII_DENYLIST_FILE, and .pii-denylist at the repo root (gitignored).

A finding is reported as path:line and rule, with the value masked, so the
guard's own output is safe in CI logs. False positives: add a regex for the
matched value to .pii-allow (tracked; must never contain personal data), or put
`pii-guard: allow` on the line. Standard library only.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRAGMA = "pii-guard: allow"
DENYLIST_FILE = ".pii-denylist"
ALLOW_FILE = ".pii-allow"

# Files whose content is machine-generated hashes and versions: the generic
# rules would only produce noise there. The deny list still applies to them.
GENERIC_SKIP = (
    "*.lock", "*lock.json", "*.lock.hcl", "requirements-*.txt", "*.min.js", "*.svg",
    "cover_letters/OpenFonts/*",
)

# Paths that must never be tracked: they exist to hold personal output.
# (pattern, exceptions)
FORBIDDEN_PATHS = (
    ("cv/main_*", ("cv/main_example.tex",)),
    ("cover_letters/[cC]over_*", ("cover_letters/cover_example.tex", "cover_letters/cover.cls")),
    ("documents/*", ("documents/README.md", "documents/*/.gitkeep")),
    ("company_research/*", ("company_research/.gitkeep", "company_research/README.md")),
    ("job_scraper/*", ("job_scraper/.gitkeep",)),
    ("*job_search_tracker.csv", ()),
    ("CLAUDE.local.md", ()),
    (".private/*", ()),
    (DENYLIST_FILE, ()),
    (".env", ()), (".env.*", (".env.example",)),
    ("*.pdf", ()), ("*.docx", ()), ("*.doc", ()), ("*.xlsx", ()), ("*.odt", ()), ("*.pages", ()),
)

SAFE_EMAIL_DOMAINS = re.compile(
    r"(^|\.)(example\.(com|org|net)|example|test|invalid|localhost|users\.noreply\.github\.com)$"
    r"|^(anthropic\.com|github\.com)$",
    re.I,
)
PLACEHOLDER_USERS = {"user", "users", "you", "your", "me", "name", "username", "runner", "runneradmin",
                     "public", "ubuntu", "root", "home", "example", "someone", "jane", "john", "alice", "bob"}

GENERIC_RULES = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")),
    ("phone", re.compile(r"(?<![\w.+-])(?:\+?\d{1,3}[ .-])?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])")),
    ("credential", re.compile(
        r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:ant-)?[A-Za-z0-9_-]{20,}"
        r"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|ops_ey[A-Za-z0-9_-]{20,}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("account-id", re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")),
    ("hostname", re.compile(r"\b([a-z0-9-]+)\.(?:workers\.dev|pages\.dev|cloudflareaccess\.com)\b", re.I)),
    ("postal-code", re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d\b")),
    ("street-address", re.compile(
        r"\b\d{1,6}[A-Za-z]?,? (?:[A-Z][a-z]+ |\d{1,3}(?:st|nd|rd|th)? ){1,3}"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|Crescent|Cres|Court|Ct|Place|Pl|Terrace|Trail)\b")),
    ("home-path", re.compile(r"(?:[A-Za-z]:\\+Users\\+|/Users/|/home/)([A-Za-z0-9._-]+)")),
)


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if out.returncode != 0:
        sys.exit(f"pii_guard: git {' '.join(args[:2])} failed: {out.stderr.decode('utf-8', 'replace').strip()}")
    return out.stdout.decode("utf-8", "replace")


def mask(value: str) -> str:
    """Never echo a finding: two leading characters and the length are enough to find it."""
    return f"{value[:2]}***({len(value)} chars)"


def _compile(lines, source: str, flags=re.I):
    rules = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append((f"{source} entry {n}", re.compile(line, flags)))
        except re.error as exc:
            sys.exit(f"pii_guard: {source} entry {n} is not a valid regex ({exc.msg})")
    return rules


def load_denylist():
    rules = []
    env = os.environ.get("PII_DENYLIST", "")
    if env.strip():
        rules += _compile(env.splitlines(), "deny list (PII_DENYLIST)")
    for label, name in (("PII_DENYLIST_FILE", os.environ.get("PII_DENYLIST_FILE")), (DENYLIST_FILE, str(ROOT / DENYLIST_FILE))):
        if name and Path(name).is_file():
            rules += _compile(Path(name).read_text(encoding="utf-8").splitlines(), f"deny list ({label})")
    return rules


def load_allow():
    path = ROOT / ALLOW_FILE
    if not path.is_file():
        return []
    return [rx for _, rx in _compile(path.read_text(encoding="utf-8").splitlines(), ALLOW_FILE, 0)]


def _skip_generic(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in GENERIC_SKIP)


def _benign(rule: str, match: re.Match) -> bool:
    text = match.group(0)
    if rule == "email":
        return bool(SAFE_EMAIL_DOMAINS.search(match.group(1))) or text.lower().startswith(("git@", "noreply@"))
    if rule == "phone":
        digits = re.sub(r"\D", "", text)
        return digits[-7:-4] == "555" or len(set(digits)) <= 2 or digits[-10:] in ("1234567890", "0123456789")
    if rule == "hostname":
        sub = match.group(1).lower()
        return any(w in sub for w in ("example", "your", "test", "demo", "sample", "replace")) or sub in ("my", "name", "team", "account")
    if rule == "home-path":
        return match.group(1).lower() in PLACEHOLDER_USERS
    if rule == "account-id":
        return len(set(text)) <= 4
    return False


def check_path(path: str):
    for pattern, exceptions in FORBIDDEN_PATHS:
        if fnmatch.fnmatch(path, pattern) and not any(fnmatch.fnmatch(path, e) for e in exceptions):
            yield f"{path}: forbidden-path (matches {pattern}; this location holds personal output and is never tracked)"
            return


def check_line(where: str, line: str, deny, allow, generic: bool = True):
    if PRAGMA in line:
        return
    rules = list(deny)
    if generic:
        rules = list(GENERIC_RULES) + rules
    for rule, rx in rules:
        for match in rx.finditer(line):
            value = match.group(0)
            if rule in dict(GENERIC_RULES) and _benign(rule, match):
                continue
            if any(a.search(value) for a in allow):
                continue
            yield f"{where}: {rule} [{mask(value)}]"


def diff_added_lines(*diff_args: str):
    """Yield (path, line number, text) for every line a diff adds, and each touched path."""
    out = git("diff", "--no-color", "--no-ext-diff", "-U0", "--diff-filter=ACMR", *diff_args)
    path, lineno = None, 0
    for raw in out.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path:
            yield path, lineno, raw[1:]
            lineno += 1


def scan_diff(deny, allow, *diff_args: str):
    names = git("diff", "--name-only", "--diff-filter=ACMR", *diff_args).splitlines()
    for path in names:
        yield from check_path(path)
    for path, lineno, text in diff_added_lines(*diff_args):
        yield from check_line(f"{path}:{lineno}", text, deny, allow, generic=not _skip_generic(path))


def scan_message(label: str, text: str, deny, allow):
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("#"):
            continue
        yield from check_line(f"{label}:{n}", line, deny, allow)


def scan_all(deny, allow):
    for path in git("ls-files", "-z").split("\0"):
        if not path:
            continue
        yield from check_path(path)
        try:
            data = (ROOT / path).read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            yield from check_line(f"{path}:{n}", line, deny, allow, generic=not _skip_generic(path))


def install_hooks() -> int:
    git("config", "core.hooksPath", ".githooks")
    print("pii_guard: git hooks enabled for this clone (core.hooksPath=.githooks)")
    if not load_denylist():
        print(f"pii_guard: no deny list yet. Create {DENYLIST_FILE} (gitignored), one regex per line. See PRIVACY.md")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--message-file")
    mode.add_argument("--range", dest="rev_range", metavar="A...B")
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--install-hooks", action="store_true")
    ap.add_argument("--require-denylist", action="store_true", help="fail when no deny list is configured")
    args = ap.parse_args(argv)

    if args.install_hooks:
        return install_hooks()

    findings = []
    if DENYLIST_FILE in git("ls-files", "--", DENYLIST_FILE).split():
        findings.append(f"{DENYLIST_FILE}: the deny list is tracked. It is personal data: git rm --cached it")
    deny, allow = load_denylist(), load_allow()
    if not deny:
        note = "no maintainer deny list configured: generic rules only (see PRIVACY.md)"
        if args.require_denylist:
            findings.append(note)
        else:
            print(("::warning::" if os.environ.get("GITHUB_ACTIONS") else "") + f"pii_guard: {note}", file=sys.stderr)

    if args.staged:
        findings += scan_diff(deny, allow, "--cached")
    elif args.message_file:
        findings += scan_message("commit message", Path(args.message_file).read_text(encoding="utf-8", errors="replace"), deny, allow)
    elif args.rev_range:
        findings += scan_diff(deny, allow, args.rev_range)
        base, _, head = args.rev_range.partition("...") if "..." in args.rev_range else args.rev_range.partition("..")
        for sha in git("rev-list", "--no-merges", f"{base}..{head or 'HEAD'}").split():
            findings += scan_message(f"commit {sha[:9]} message", git("log", "-1", "--format=%B", sha), deny, allow)
    else:
        findings += scan_all(deny, allow)

    findings = list(dict.fromkeys(findings))
    if findings:
        print(f"pii_guard: {len(findings)} finding(s). Values are masked on purpose.")
        for f in findings:
            print(("::error::" if os.environ.get("GITHUB_ACTIONS") else "  ") + f)
        print("Remove the personal data (it belongs in a private repo, see PRIVACY.md). "
              f"For a false positive, add a regex to {ALLOW_FILE} or `{PRAGMA}` on the line.")
        return 1
    print(f"pii_guard: OK ({len(deny)} deny-list entries, {len(GENERIC_RULES)} generic rules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
