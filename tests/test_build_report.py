"""build_report: upstream's report plus the events-log add-on; escaped, rates suppressed under n=5, CSV-only mode."""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_report as b  # noqa: E402
import tracker as t  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "tracker" / "examples" / "events.csv"


def build(*extra, events=SAMPLE, root=None):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d, "site")
        rc = b.main(["--root", str(root or Path(d, "empty")), "--events", str(events), "--out", str(out), *extra])
        return rc, (out / "index.html").read_text(encoding="utf-8") if rc == 0 else ""


def ev(**kw):
    return {**{c: "" for c in t.EVENT_COLUMNS}, **kw}


class Report(unittest.TestCase):
    def test_same_input_same_bytes_on_one_day(self):
        self.assertEqual(build("--floor", "175000"), build("--floor", "175000"))

    def test_sample_content_and_links(self):
        rc, html = build("--floor", "175000", "--repo-url", "https://github.com/example/demo")
        self.assertEqual(rc, 0)
        for s in ("Demo Systems", "Response rate", "175,000 CAD", "https://github.com/example/demo/blob/"):
            self.assertIn(s, html)
        self.assertNotIn("src=", html)  # self-contained: no external script, image or font

    def test_no_repo_url_means_no_links_to_artifacts(self):
        self.assertNotIn("/blob/", build()[1])

    def test_escaping(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "e.csv")
            t.write_events([ev(date="2026-01-01", id="x", type="found", company="<script>alert(1)</script>", role="R"),
                            ev(date="2026-01-01", id="x", type="set", field="url", value="https://e.com/")], log)
            rc, html = build(events=log)
        self.assertEqual(rc, 0)
        self.assertNotIn("<script>alert", html)

    def test_rates_suppressed_under_five(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "e.csv")
            t.write_events([ev(date="2026-01-01", id="x", type=typ, company="A", role="B",
                               **({"field": "url", "value": "https://e.com"} if typ == "set" else {}))
                            for typ in ("found", "set", "validated", "decided_to_apply", "applied")], log)
            rc, html = build(events=log)
        self.assertIn("n&lt;5", html)

    def test_csv_only_mode_is_upstreams_page_without_the_addon(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "job_search_tracker.csv").write_text(
                ",".join(t.LEGACY_COLS) + "\n2026-01-01,Acme,Tech,Architect,,portal,interview,,70,,,,https://e.com,\n", encoding="utf-8")
            rc, html = build(events=Path(d, "none.csv"), root=d)
        self.assertEqual(rc, 0)
        self.assertIn("Acme", html)
        self.assertNotIn("From the events log", html)

    def test_archive_copy_is_dated_and_overwritten_the_same_day(self):
        with tempfile.TemporaryDirectory() as d:
            arch = Path(d, "reports", "archive")
            arch.mkdir(parents=True)
            for _ in range(2):
                self.assertEqual(b.main(["--root", d, "--events", str(SAMPLE), "--out", str(Path(d, "site"))]), 0)
            self.assertEqual([p.name for p in arch.iterdir()], [f"{date.today().isoformat()}.html"])
            self.assertEqual(build()[1].count("Demo Systems") > 0, True)

    def test_no_archive_folder_means_no_archive(self):
        with tempfile.TemporaryDirectory() as d:
            b.main(["--root", d, "--events", str(SAMPLE), "--out", str(Path(d, "site"))])
            self.assertFalse(Path(d, "reports").exists())

    def test_missing_inputs(self):
        self.assertEqual(b.main(["--root", "/nope", "--events", "/nope.csv", "--out", "/tmp/claude-0/x"]), 2)


def rec(status, applied=None, responded=None):
    return {"company": "Acme", "role": "SA", "status": status, "applied_on": applied, "responded_on": responded,
            "comp_max": None, "currency": "", "artifacts": []}


class Stats(unittest.TestCase):
    def test_a_drafted_role_is_never_counted_as_sent(self):
        self.assertEqual(b.stats([rec("drafted")], None, None)["sent"], 0)

    def test_median_days_to_reply(self):
        records = [rec("applied", "2026-01-10", "2026-01-12") for _ in range(5)]
        self.assertEqual(b.stats(records, None, None)["median_days"], 2)

    def test_a_reply_before_the_application_is_not_a_response_time(self):
        role = {"id": "x", "company": "X", "role": "SA", "status": "applied", "found_on": "2026-01-01", "history": [],
                "events": [], "actions": [{"date": "2026-01-03", "type": "response_received"},
                                          {"date": "2026-01-10", "type": "applied"}]}
        self.assertIsNone(b.records_from_events([role])[0]["responded_on"])

    def test_rejected_counts_as_a_response(self):
        records = [rec("rejected", "2026-01-01") for _ in range(5)]
        self.assertEqual(b.stats(records, None, None)["response_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
