"""build_report: deterministic, escaped, rates suppressed under n=5, falls back to the upstream CSV."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_report as b  # noqa: E402
import tracker as t  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "tracker" / "examples" / "events.csv"


def build(*extra, events=SAMPLE, csv_path="/nonexistent.csv"):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d, "site")
        rc = b.main(["--events", str(events), "--csv", csv_path, "--applications", d, "--out", str(out), *extra])
        return rc, (out / "index.html").read_text(encoding="utf-8") if rc == 0 else ""


class Report(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(build("--floor", "175000"), build("--floor", "175000"))

    def test_sample_content_and_links(self):
        rc, html = build("--floor", "175000", "--repo-url", "https://github.com/example/demo")
        self.assertEqual(rc, 0)
        for s in ("Demo Systems", "Response rate", "Funnel", "175,000 CAD", "https://github.com/example/demo/blob/"):
            self.assertIn(s, html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertNotIn("src=", html)  # self-contained: no external script, image or font

    def test_no_repo_url_means_no_links_to_artifacts(self):
        self.assertNotIn("/blob/", build()[1])

    def test_escaping(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "e.csv")
            t.write_events([
                {**{c: "" for c in t.EVENT_COLUMNS}, "date": "2026-01-01", "id": "x", "type": "found",
                 "company": "<script>alert(1)</script>", "role": 'R"&\''},
                {**{c: "" for c in t.EVENT_COLUMNS}, "date": "2026-01-01", "id": "x", "type": "set", "field": "url",
                 "value": 'https://e.com/"onmouseover="x'}], log)
            rc, html = build(events=log)
        self.assertEqual(rc, 0)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn('"onmouseover="', html)

    def test_rates_suppressed_under_five(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "e.csv")
            rows = []
            for typ in ("found", "set", "validated", "decided_to_apply", "applied"):
                rows.append({**{c: "" for c in t.EVENT_COLUMNS}, "date": "2026-01-01", "id": "x", "type": typ,
                             "company": "A", "role": "B", **({"field": "url", "value": "https://e.com"} if typ == "set" else {})})
            t.write_events(rows, log)
            rc, html = build(events=log)
        self.assertIn("n&lt;5", html)

    def test_csv_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "job_search_tracker.csv")
            f.write_text(",".join(t.LEGACY_COLS) + "\n2026-01-01,Acme,Tech,Architect,,portal,interview,,70,,,,https://e.com,\n", encoding="utf-8")
            rc, html = build(events=Path(d, "none.csv"), csv_path=str(f))
        self.assertEqual(rc, 0)
        self.assertIn("Acme", html)

    def test_missing_inputs(self):
        self.assertEqual(b.main(["--events", "/nope.csv", "--csv", "/nope2.csv", "--out", "/tmp/claude-0/x"]), 2)


if __name__ == "__main__":
    unittest.main()
