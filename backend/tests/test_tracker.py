import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import config, tracker


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.previous_data_dir = config.DATA_DIR
        self.previous_output_dir = config.OUTPUT_DIR
        self.tempdir = tempfile.TemporaryDirectory(prefix="resume-tracker-tests-")
        root = Path(self.tempdir.name)
        config.DATA_DIR = root / "data"
        config.OUTPUT_DIR = root / "output"

    def tearDown(self):
        config.DATA_DIR = self.previous_data_dir
        config.OUTPUT_DIR = self.previous_output_dir
        self.tempdir.cleanup()

    def record(self, company: str = "Acme") -> str:
        return tracker.record_compiled(
            company=company,
            role="Engineer",
            filename="Resume_Acme_Engineer.pdf",
            edits_applied=3,
            fit_score=78,
            keywords_total=12,
            keywords_matched=9,
        )

    def test_compiled_event_round_trips_all_fields(self):
        application_id = self.record()
        [application] = tracker.read_applications()
        self.assertEqual(application["id"], application_id)
        self.assertEqual(application["company"], "Acme")
        self.assertEqual(application["edits_applied"], 3)
        self.assertEqual(application["fit_score"], 78)
        self.assertEqual(application["status"], "compiled")

    def test_last_outcome_wins(self):
        application_id = self.record()
        tracker.record_outcome(application_id[:8], "screen", "phone screen")
        tracker.record_letter(company="Letter Only", role="Writer", filename="CoverLetter.pdf")
        tracker.record_outcome("latest", "interview", "onsite")
        application = tracker.read_applications()[0]
        self.assertEqual(application["status"], "interview")
        self.assertEqual(application["note"], "onsite")

    def test_malformed_line_is_skipped(self):
        self.record()
        with (config.DATA_DIR / "applications.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("not-json\n")
        self.assertEqual(len(tracker.read_applications()), 1)

    def test_id_prefix_resolution_rejects_ambiguity(self):
        fake_ids = [type("Id", (), {"hex": "abc111"})(), type("Id", (), {"hex": "abc222"})()]
        with patch("backend.app.tracker.uuid.uuid4", side_effect=fake_ids):
            self.record("One")
            self.record("Two")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            tracker.record_outcome("abc", "applied")
        tracker.record_outcome("abc1", "applied")
        self.assertEqual(tracker.read_applications()[0]["status"], "applied")

    def test_report_html_escapes_untrusted_fields(self):
        self.record("<script>alert(1)</script>")
        destination = tracker.generate_report()
        report = destination.read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", report)
        self.assertNotIn("<script>alert(1)</script>", report)


if __name__ == "__main__":
    unittest.main()
