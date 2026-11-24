"""build_report: the glue derives the CSV from the events log and renders upstream's page."""
import sys
import tempfile
import unittest
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

    def test_missing_inputs(self):
        self.assertEqual(b.main(["--root", "/nope", "--events", "/nope.csv", "--out", "/tmp/claude-0/x"]), 2)


if __name__ == "__main__":
    unittest.main()
