"""tools/pii_guard.py: generic rules, deny list handling, masking, forbidden paths."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pii_guard", REPO / "tools" / "pii_guard.py")
pii_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pii_guard)


def rules_hit(line, deny=(), allow=()):
    return [f.split(": ", 1)[1].split(" [")[0] for f in pii_guard.check_line("f:1", line, list(deny), list(allow))]


class GenericRuleTests(unittest.TestCase):
    def test_flags_personal_shapes(self):
        cases = {
            "email": "contact: jane.roe@" + "corp-mail.io",
            "phone": "call (416) " + "867-5309 today",
            "credential": "token ghp_" + "a" * 36,
            "account-id": "account_id = " + "0a1b2c3d4e5f6071" + "8293a4b5c6d7e8f9",
            "hostname": "https://tracker-prod.acme." + "workers.dev/",
            "postal-code": "Toronto ON M5V" + " 3L9",
            "street-address": "lives at 4821 Maple" + " Avenue",
            "home-path": "C:\\Users\\" + "jroe\\Docs",
        }
        for rule, line in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, rules_hit(line))

    def test_placeholders_and_fixtures_pass(self):
        for line in (
            "mail user@example.com or 1234567+bot@users.noreply.github.com",
            "phone (555) 555-0123",
            "https://tracker.example.workers.dev and REPLACE_WITH_ID.workers.dev",
            "/home/user/project and /Users/you/x and C:\\Users\\runneradmin\\a",
            "sha256:" + "ab" * 32,
            "date 2026-11-14 version 1.10.6",
        ):
            with self.subTest(line=line):
                self.assertEqual(rules_hit(line), [])

    def test_pragma_and_allow_file_suppress(self):
        line = "contact: jane.roe@" + "corp-mail.io"
        self.assertEqual(rules_hit(line + "  # pii-guard: allow"), [])
        self.assertEqual(rules_hit(line, allow=[re.compile(r"@corp-mail\.io$")]), [])

    def test_lockfiles_skip_generic_rules_but_not_the_deny_list(self):
        self.assertTrue(pii_guard._skip_generic("app/package-lock.json"))
        deny = pii_guard._compile(["zanzibar"], "deny list (test)")
        hits = list(pii_guard.check_line("x.lock:1", "resolved Zanzibar", deny, [], generic=False))
        self.assertEqual(len(hits), 1)


class DenyListTests(unittest.TestCase):
    def test_finding_never_echoes_the_value_or_the_pattern(self):
        deny = pii_guard._compile(["# comment", "", "Quentin Marlowe"], "deny list (test)")
        (finding,) = pii_guard.check_line("a.md:3", "by quentin marlowe", deny, [])
        self.assertIn("deny list (test) entry 3", finding)
        self.assertNotIn("uentin", finding.lower())
        self.assertNotIn("marlowe", finding.lower())

    def test_reads_env_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "deny.txt"
            f.write_text("alpha\nbeta\n", encoding="utf-8")
            old = dict(os.environ)
            os.environ.update(PII_DENYLIST="gamma", PII_DENYLIST_FILE=str(f))
            try:
                self.assertGreaterEqual(len(pii_guard.load_denylist()), 3)
            finally:
                os.environ.clear()
                os.environ.update(old)


class ForbiddenPathTests(unittest.TestCase):
    def test_personal_output_paths_are_refused(self):
        for path in ("cv/main_acme_architect.tex", "cover_letters/cover_acme.tex", "documents/cv/master.md",
                     "CLAUDE.local.md", ".private/profile.md", ".pii-denylist", "x/resume.pdf", ".env.local"):
            with self.subTest(path=path):
                self.assertTrue(list(pii_guard.check_path(path)))

    def test_stock_files_are_allowed(self):
        for path in ("cv/main_example.tex", "cover_letters/cover_example.tex", "cover_letters/cover.cls",
                     "CLAUDE.md", "documents/README.md", "documents/cv/.gitkeep", "tools/pii_guard.py", "CLAUDE.template.md"):
            with self.subTest(path=path):
                self.assertEqual(list(pii_guard.check_path(path)), [])


class RepoStateTests(unittest.TestCase):
    def run_guard(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PII_DENYLIST")}
        return subprocess.run([sys.executable, str(REPO / "tools" / "pii_guard.py"), *args],
                              capture_output=True, text=True, env=env, cwd=REPO)

    def test_tracked_tree_is_clean(self):
        result = self.run_guard("--all")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_deny_list_and_overlay_are_ignored_by_git(self):
        for path in (".pii-denylist", ".private/x.md"):
            with self.subTest(path=path):
                r = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO)
                self.assertEqual(r.returncode, 0)

    def test_hooks_call_the_guard_and_ci_runs_it(self):
        for hook, flag in (("pre-commit", "--staged"), ("commit-msg", "--message-file")):
            text = (REPO / ".githooks" / hook).read_text(encoding="utf-8")
            self.assertIn("tools/pii_guard.py", text)
            self.assertIn(flag, text)
        ci = "".join(f.read_text(encoding="utf-8") for f in (REPO / ".github" / "workflows").glob("*.yml"))
        self.assertIn("tools/pii_guard.py --range", ci)
        self.assertIn("tools/pii_guard.py --all", ci)


if __name__ == "__main__":
    unittest.main()
