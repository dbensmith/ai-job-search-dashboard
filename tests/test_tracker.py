"""Events log: the sample must validate, transitions are enforced while folding, and the CSV is derived."""
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import tracker as t  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "tracker" / "examples" / "events.csv"


def ev(date, rid, typ, **kw):
    row = {c: "" for c in t.EVENT_COLUMNS}
    row.update(date=date, id=rid, type=typ, _line=0, **kw)
    return row


def found(rid="acme_sa", date="2026-11-01"):
    return [ev(date, rid, "found", company="Acme", role="Security Architect"),
            ev(date, rid, "set", field="url", value="https://example.com/j"),
            ev(date, rid, "validated")]


class Sample(unittest.TestCase):
    def test_sample_validates(self):
        self.assertEqual(t.validate(t.read_events(SAMPLE)), [])

    def test_report_renders_sample(self):
        self.assertIn("Acme Cloud", t.export_md(t.load_roles(SAMPLE)))


class Rules(unittest.TestCase):
    def errs(self, rows):
        return t.validate(rows)

    def test_valid_path(self):
        rows = found() + [ev("2026-11-02", "acme_sa", "drafted"), ev("2026-11-03", "acme_sa", "applied"),
                          ev("2026-11-10", "acme_sa", "interview", value="recruiter_screen")]
        self.assertEqual(self.errs(rows), [])
        r = t.fold(rows)[0][0]
        self.assertEqual((r["status"], r["stage"]), ("interview", "recruiter_screen"))

    def test_cannot_draft_unvalidated(self):
        rows = found()[:2] + [ev("2026-11-02", "acme_sa", "drafted")]
        self.assertTrue(any("employer-page check" in e for e in self.errs(rows)))

    def test_illegal_transition(self):
        rows = found() + [ev("2026-11-02", "acme_sa", "offer")]
        self.assertTrue(any("not allowed while found" in e for e in self.errs(rows)))

    def test_final_is_terminal(self):
        rows = found() + [ev("2026-11-02", "acme_sa", "skipped", value="location"),
                          ev("2026-11-03", "acme_sa", "decided_to_apply")]
        self.assertTrue(any("not allowed while skipped" in e for e in self.errs(rows)))

    def test_skipped_needs_reason(self):
        rows = found() + [ev("2026-11-02", "acme_sa", "skipped")]
        self.assertTrue(any("skip reason" in e for e in self.errs(rows)))

    def test_event_before_found_and_duplicate_found(self):
        self.assertTrue(any("before found" in e for e in self.errs([ev("2026-11-01", "x", "applied")])))
        self.assertTrue(any("duplicate found" in e for e in self.errs(found() + found())))

    def test_dates_must_not_go_backwards(self):
        rows = found() + [ev("2026-11-05", "acme_sa", "decided_to_apply"), ev("2026-11-04", "acme_sa", "drafted")]
        self.assertTrue(any("earlier than" in e for e in self.errs(rows)))

    def test_unknown_type_field_and_bad_comp(self):
        self.assertTrue(any("unknown event type" in e for e in self.errs([ev("2026-11-01", "x", "nonsense")])))
        self.assertTrue(any("not an editable" in e for e in self.errs(found() + [ev("2026-11-02", "acme_sa", "set", field="status", value="applied")])))
        rows = found() + [ev("2026-11-02", "acme_sa", "set", field="comp_min", value="100")]
        self.assertTrue(any("comp_currency" in e for e in self.errs(rows)))

    def test_referral_needs_no_url(self):
        rows = [ev("2026-11-01", "ref", "found", company="Acme", role="General"),
                ev("2026-11-01", "ref", "set", field="referral", value="true"),
                ev("2026-11-02", "ref", "validated"), ev("2026-11-02", "ref", "decided_to_apply"),
                ev("2026-11-03", "ref", "applied")]
        self.assertEqual(self.errs(rows), [])

    def test_role_closed_sets_closed_on_without_status_change(self):
        r = t.fold(found() + [ev("2026-11-05", "acme_sa", "role_closed")])[0][0]
        self.assertEqual((r["closed_on"], r["status"]), ("2026-11-05", "found"))


class Derived(unittest.TestCase):
    def test_csv_is_upstream_header_and_skips_unsubmitted(self):
        rows = found() + [ev("2026-11-02", "acme_sa", "drafted"), ev("2026-11-03", "acme_sa", "applied")]
        out = list(csv.DictReader(io.StringIO(t.legacy_csv(t.fold(rows)[0]))))
        self.assertEqual(list(out[0]), t.LEGACY_COLS)
        self.assertEqual((out[0]["date"], out[0]["status"]), ("2026-11-02", "applied"))
        self.assertEqual(t.legacy_csv(t.fold(found())[0]).count("\n"), 1)  # header only: found is not an application

    def test_append_validates_and_regenerates_csv(self):
        with tempfile.TemporaryDirectory() as d:
            log, out = Path(d, "events.csv"), Path(d, "job_search_tracker.csv")
            self.assertEqual(t.main(["--events", str(log), "--csv", str(out), "add", "--company", "Acme",
                                     "--role", "Security Architect", "--url", "https://example.com/j", "--date", "2026-11-01"]), 0)
            rid = t.fold(t.read_events(log))[0][0]["id"]
            for typ in ("validated", "drafted"):
                self.assertEqual(t.main(["--events", str(log), "--csv", str(out), "event", rid, typ, "--date", "2026-11-02"]), 0)
            before = log.read_text(encoding="utf-8")
            with self.assertRaises(SystemExit):
                t.main(["--events", str(log), "--csv", str(out), "event", rid, "offer", "--date", "2026-11-03"])
            self.assertEqual(log.read_text(encoding="utf-8"), before)  # a refused write changes nothing
            self.assertIn("drafted", out.read_text(encoding="utf-8"))
            self.assertTrue(before.startswith(",".join(t.EVENT_COLUMNS)))


if __name__ == "__main__":
    unittest.main()
