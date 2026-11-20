"""tracker_submit: submit appends events, is idempotent, and check-packs fails on unrecorded submissions."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import tracker as t  # noqa: E402
import tracker_submit as ts  # noqa: E402

PACK = "date,company,role,status\n2026-01-01,Acme,Security Architect,{}\n"


class Submit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.log, self.csv, self.packs = d / "events.csv", d / "job_search_tracker.csv", d / "packs"
        (self.packs / "acme").mkdir(parents=True)
        t.main(["--events", str(self.log), "--csv", str(self.csv), "add", "--company", "Acme",
                "--role", "Security Architect", "--url", "https://example.com/j", "--date", "2026-11-01"])
        self.rid = t.fold(t.read_events(self.log))[0][0]["id"]
        t.main(["--events", str(self.log), "--csv", str(self.csv), "event", self.rid, "validated", "--date", "2026-11-01"])

    def tearDown(self):
        self.tmp.cleanup()

    def roles(self):
        return t.load_roles(self.log)

    def test_submit_from_found_passes_through_considering(self):
        done = ts.submit(self.log, self.csv, self.rid, "2026-11-03", artifact="documents/applications/a/cv.pdf", commit="abc1234")
        self.assertEqual(done["status"], "applied")
        self.assertEqual([h["status"] for h in done["history"]], ["found", "considering", "applied"])
        last = t.read_events(self.log)[-1]
        self.assertEqual((last["type"], last["artifact"], last["commit"]), ("applied", "documents/applications/a/cv.pdf", "abc1234"))
        self.assertIn("applied", self.csv.read_text(encoding="utf-8"))

    def test_submit_twice_records_once(self):
        ts.submit(self.log, self.csv, self.rid, "2026-11-03")
        n = len(t.read_events(self.log))
        ts.submit(self.log, self.csv, self.rid, "2026-11-04")
        self.assertEqual(len(t.read_events(self.log)), n)

    def test_check_packs(self):
        (self.packs / "acme" / "tracker.csv").write_text(PACK.format("applied"), encoding="utf-8")
        self.assertEqual(len(ts.check_packs(self.packs, self.roles())), 1)
        ts.submit(self.log, self.csv, self.rid, "2026-11-03")
        self.assertEqual(ts.check_packs(self.packs, self.roles()), [])
        (self.packs / "acme" / "tracker.csv").write_text(PACK.format("drafted"), encoding="utf-8")
        self.assertEqual(ts.check_packs(self.packs, []), [])  # drafted rows are not submissions

    def test_unknown_pack_role_flagged(self):
        (self.packs / "acme" / "tracker.csv").write_text(PACK.format("rejected"), encoding="utf-8")
        self.assertIn("not in the events log", ts.check_packs(self.packs, [])[0])

    def test_cli_exit_codes(self):
        base = ["--events", str(self.log), "--csv", str(self.csv)]
        self.assertEqual(ts.main(base + ["submit", self.rid, "--date", "nope"]), 2)
        self.assertEqual(ts.main(base + ["check-packs", str(self.packs / "missing")]), 2)
        (self.packs / "acme" / "tracker.csv").write_text(PACK.format("applied"), encoding="utf-8")
        self.assertEqual(ts.main(base + ["check-packs", str(self.packs)]), 1)


if __name__ == "__main__":
    unittest.main()
