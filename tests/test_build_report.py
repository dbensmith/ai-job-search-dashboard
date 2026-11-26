"""build_report: the glue derives the CSV from the events log and renders upstream's page."""
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_report as b  # noqa: E402
import tracker as t  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "tracker" / "examples" / "events.csv"


def build(events=SAMPLE, root=None):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d, "site")
        rc = b.main(["--root", str(root or Path(d, "empty")), "--events", str(events), "--out", str(out)])
        return rc, (out / "index.html").read_text(encoding="utf-8") if rc == 0 else ""


class Report(unittest.TestCase):
    def test_sample_renders_and_is_self_contained(self):
        rc, html = build()
        self.assertEqual(rc, 0)
        self.assertIn("Demo Systems", html)
        self.assertNotIn("src=", html)

    def test_same_input_same_bytes_on_one_day(self):
        self.assertEqual(build(), build())

    def test_escaping(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "e.csv")
            row = {c: "" for c in t.EVENT_COLUMNS}
            t.write_events([{**row, "date": "2026-01-01", "id": "x", "type": "found",
                             "company": "<script>alert(1)</script>", "role": "R"}], log)
            rc, html = build(events=log)
        self.assertEqual(rc, 0)
        self.assertNotIn("<script>alert", html)

    def test_csv_only_mode(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "job_search_tracker.csv").write_text(
                ",".join(t.LEGACY_COLS) + "\n2026-01-01,Acme,Tech,Architect,,portal,interview,,70,,,,https://e.com,\n", encoding="utf-8")
            rc, html = build(events=Path(d, "none.csv"), root=d)
        self.assertEqual(rc, 0)
        self.assertIn("Acme", html)

    def test_outcome_ticks_count_without_symlinks(self):
        # Windows refuses symlinks without Developer Mode (WinError 1314); the report must not need one.
        with tempfile.TemporaryDirectory() as d:
            pack = Path(d, "documents", "applications", "fabrikam_sample_senior_security_architect")
            pack.mkdir(parents=True)
            (pack / "outcome.md").write_text(
                "## Interview stages reached\n- [x] Phone screen\n", encoding="utf-8")
            seen = {}
            real = b.report_data.load

            def capture(root):
                seen["data"] = real(root)
                return seen["data"]

            with mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "privilege not held")), \
                    mock.patch.object(b.report_data, "load", side_effect=capture):
                rc, _ = build(root=d)
        self.assertEqual(rc, 0)
        row = next(r for r in seen["data"]["rows"] if r["company"] == "Fabrikam Sample")
        self.assertEqual(row["status"], "applied")
        self.assertGreaterEqual(row["_stage"], 1)  # the ticked phone screen reached the funnel

    def test_missing_inputs(self):
        self.assertEqual(b.main(["--root", "/nope", "--events", "/nope.csv", "--out", "/tmp/claude-0/x"]), 2)


if __name__ == "__main__":
    unittest.main()
