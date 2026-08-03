import unittest
from unittest.mock import patch

import fakeredis

from backend.app import config, jobs
from backend.app.models import (
    EditTarget,
    ExtractKeywordsResponse,
    FitReport,
    Keyword,
    KeywordMatch,
    ReviewedEdit,
    ReviewedParagraph,
)


class JobStorageTests(unittest.TestCase):
    def setUp(self):
        self.client = fakeredis.FakeRedis(decode_responses=True)
        self.client_patcher = patch.object(jobs, "_client", return_value=self.client)
        self.client_patcher.start()
        self.addCleanup(self.client_patcher.stop)

    def test_create_and_get_round_trip_defaults(self):
        job_id = jobs.create_job()
        stored = jobs.get_job(job_id)

        self.assertIsNotNone(stored)
        self.assertEqual(stored.kind, "tailor")
        self.assertEqual(stored.status, "running")
        self.assertEqual(stored.step, "Extracting role requirements…")
        self.assertIsNone(stored.analysis)
        self.assertEqual(stored.edits, [])
        self.assertIsNone(stored.fit)
        self.assertEqual(stored.paragraphs, [])
        self.assertIsNone(stored.error)

    def test_nested_models_round_trip_after_update(self):
        job_id = jobs.create_job()
        analysis = ExtractKeywordsResponse(
            company="Example Co",
            role="Software Engineer",
            keywords=[
                Keyword(
                    term="Python",
                    category="technology",
                    importance="high",
                    evidence="Python is required",
                )
            ],
        )
        edit = ReviewedEdit(
            target=EditTarget(
                section="Projects",
                anchor="Game Outcome Prediction Platform",
                item_index=0,
            ),
            new_text="Built a Python model.",
            reason="Surface Python work.",
            original_text="Built a model.",
            traceable=True,
            issues=[],
        )
        matched = KeywordMatch(
            term="Python",
            category="technology",
            importance="high",
            matched=True,
        )
        fit = FitReport(score=100, matched=[matched], missing=[])
        paragraph = ReviewedParagraph(text="I build grounded software.", issues=[])

        jobs.update_job(
            job_id,
            status="done",
            analysis=analysis,
            edits=[edit],
            fit=fit,
            paragraphs=[paragraph],
        )
        stored = jobs.get_job(job_id)

        self.assertEqual(stored.status, "done")
        self.assertEqual(stored.analysis, analysis)
        self.assertEqual(stored.edits, [edit])
        self.assertEqual(stored.fit, fit)
        self.assertEqual(stored.paragraphs, [paragraph])

    def test_update_unknown_job_is_silent_no_op(self):
        jobs.update_job("unknown", status="done")
        self.assertIsNone(jobs.get_job("unknown"))

    def test_get_unknown_job_returns_none(self):
        self.assertIsNone(jobs.get_job("unknown"))

    def test_job_key_has_configured_ttl(self):
        job_id = jobs.create_job()
        ttl = self.client.ttl(jobs._key(job_id))
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, config.JOB_STATE_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
