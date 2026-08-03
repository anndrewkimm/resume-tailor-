import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from backend.app import config, digest, discovery, email_tracker, tracker


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.previous_data_dir = config.DATA_DIR
        self.previous_output_dir = config.OUTPUT_DIR
        self.tempdir = tempfile.TemporaryDirectory(prefix="resume-digest-tests-")
        root = Path(self.tempdir.name)
        config.DATA_DIR = root / "data"
        config.OUTPUT_DIR = root / "output"

    def tearDown(self):
        config.DATA_DIR = self.previous_data_dir
        config.OUTPUT_DIR = self.previous_output_dir
        self.tempdir.cleanup()

    def test_empty_digest_is_self_contained_and_actionable(self):
        with patch("backend.app.digest._today", return_value=date(2026, 8, 2)):
            destination = digest.generate_digest()
        report = destination.read_text(encoding="utf-8")

        self.assertEqual(destination, config.OUTPUT_DIR / "digest_report.html")
        self.assertIn("No unhandled discovered jobs", report)
        self.assertIn("No applications tracked yet", report)
        self.assertIn("No email suggestions need review", report)
        self.assertIn("role=\"img\"", report)
        self.assertNotIn("<script", report)
        self.assertNotIn("http://", report)
        self.assertNotIn("https://", report)

    def test_digest_combines_logs_sorts_priority_and_escapes_untrusted_text(self):
        with patch(
            "backend.app.discovery._timestamp",
            side_effect=["2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00"],
        ):
            discovery.record_seen(
                id="unsafe:1",
                company="<script>alert(1)</script>",
                role="Unsafe role",
                location="Remote",
                url="javascript:alert(1)",
                platform="greenhouse",
                description="Description",
            )
            discovery.record_seen(
                id="safe:2",
                company="Safe Co",
                role="High-fit role",
                location="Chicago",
                url="https://example.com/jobs/2",
                platform="lever",
                description="Description",
            )
        discovery.record_fit("unsafe:1", fit_score=20, keywords_total=1, keywords_matched=0)
        discovery.record_fit("safe:2", fit_score=90, keywords_total=1, keywords_matched=1)
        with patch("backend.app.tracker._timestamp", return_value="2026-08-02T01:00:00+00:00"):
            application_id = tracker.record_compiled(
                company="Safe Co",
                role="High-fit role",
                filename="Resume.pdf",
                edits_applied=1,
                fit_score=90,
                keywords_total=1,
                keywords_matched=1,
            )
        application = tracker.read_applications()[0]
        with patch(
            "backend.app.email_tracker._timestamp",
            return_value="2026-08-02T02:00:00+00:00",
        ):
            email_tracker._record_suggested(
                "gmail123",
                application=application,
                status="interview",
                evidence="<b>keyword evidence</b>",
                sender="jobs@example.com",
                subject="Update",
            )
        suggestion_log_before = (
            config.DATA_DIR / "email_suggestions.jsonl"
        ).read_text(encoding="utf-8")

        with patch("backend.app.digest._today", return_value=date(2026, 8, 2)):
            destination = digest.generate_digest()
        report = destination.read_text(encoding="utf-8")

        self.assertLess(report.index("High-fit role"), report.index("Unsafe role"))
        self.assertIn('href="https://example.com/jobs/2"', report)
        self.assertIn('href="#"', report)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", report)
        self.assertNotIn("<script>alert(1)</script>", report)
        self.assertIn("&lt;b&gt;keyword evidence&lt;/b&gt;", report)
        self.assertIn(
            "python -m backend.app.email_tracker confirm gmail123",
            report,
        )
        self.assertIn(application_id, (config.DATA_DIR / "applications.jsonl").read_text())
        self.assertIn("Discovered: 2", report)
        self.assertIn("Applications: 1", report)
        self.assertIn("Email suggestions: 1", report)
        self.assertEqual(
            (config.DATA_DIR / "email_suggestions.jsonl").read_text(encoding="utf-8"),
            suggestion_log_before,
        )

        email_tracker.dismiss_suggestion("gmail123")
        resolved_report = digest.generate_digest().read_text(encoding="utf-8")
        self.assertNotIn(
            "python -m backend.app.email_tracker confirm gmail123",
            resolved_report,
        )

    def test_funnel_renders_only_present_statuses_with_proportional_widths(self):
        section = digest._funnel_section(
            [
                {"status": "compiled"},
                {"status": "compiled"},
                {"status": "interview"},
            ]
        )

        self.assertIn("Compiled", section)
        self.assertIn("Interview", section)
        self.assertIn("width:100.0%", section)
        self.assertIn("width:50.0%", section)
        self.assertNotIn("Offer", section)

    def test_discovery_rows_are_capped_at_five_for_actionable_focus(self):
        postings = [
            {
                "fit_score": score,
                "company": f"Company {score}",
                "role": f"Role {score}",
                "url": f"https://example.com/{score}",
            }
            for score in range(10)
        ]

        rows = digest._discovery_rows(postings)

        self.assertEqual(rows.count("<tr>"), 5)
        self.assertIn("Role 9", rows)
        self.assertNotIn("Role 4", rows)

    def test_activity_counts_ignore_events_outside_window_and_invalid_dates(self):
        with patch("backend.app.digest._today", return_value=date(2026, 8, 2)):
            days, series = digest._activity_counts(
                [
                    {"at": "2026-08-02T12:00:00+00:00"},
                    {"at": "2020-01-01T00:00:00+00:00"},
                    {"at": "invalid"},
                ],
                [{"at": "2026-08-01T00:00:00+00:00"}],
                [],
            )
        self.assertEqual(len(days), 30)
        self.assertEqual(sum(series[0]), 1)
        self.assertEqual(sum(series[1]), 1)
        self.assertEqual(sum(series[2]), 0)


if __name__ == "__main__":
    unittest.main()
