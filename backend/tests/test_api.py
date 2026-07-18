import unittest
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import config
from backend.app.latex_compile import CompileResult
from backend.app.main import app
from backend.app.models import (
    CoverLetterDraftResponse,
    EditTarget,
    ExtractKeywordsResponse,
    Keyword,
    LetterParagraph,
    ProposedEdit,
)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_secret = config.SHARED_SECRET
        self.previous_output_dir = config.OUTPUT_DIR
        self.previous_data_dir = config.DATA_DIR
        self.output_tempdir = tempfile.TemporaryDirectory(prefix="resume-tailor-tests-")
        self.data_tempdir = tempfile.TemporaryDirectory(prefix="resume-tracker-api-tests-")
        config.SHARED_SECRET = "test-secret"
        config.OUTPUT_DIR = Path(self.output_tempdir.name)
        config.DATA_DIR = Path(self.data_tempdir.name)
        self.client = TestClient(app)
        self.headers = {"X-Extension-Secret": "test-secret"}

    def tearDown(self):
        config.SHARED_SECRET = self.previous_secret
        config.OUTPUT_DIR = self.previous_output_dir
        config.DATA_DIR = self.previous_data_dir
        self.output_tempdir.cleanup()
        self.data_tempdir.cleanup()

    def test_health_is_available(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["resume_found"])

    def test_backend_uses_explicit_local_env_path(self):
        self.assertEqual(config.ENV_PATH, config.REPO_ROOT / "backend" / ".env")

    def test_protected_endpoint_rejects_bad_secret(self):
        response = self.client.post("/compile", json={})
        self.assertEqual(response.status_code, 403)

    def test_firefox_extension_origin_is_allowed_by_cors(self):
        origin = "moz-extension://12345678-1234-1234-1234-123456789abc"
        response = self.client.options(
            "/compile",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-extension-secret",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], origin)

    def test_regular_webpage_origin_is_not_allowed_by_cors(self):
        response = self.client.options(
            "/compile",
            headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    @patch("backend.app.main.compile_tex", return_value=CompileResult(pdf_bytes=b"%PDF-test"))
    def test_compile_applies_safe_approved_edit(self, compile_mock):
        payload = {
            "company": "Example, Inc.",
            "role": "Software Engineer",
            "approved_edits": [
                {
                    "target": {"section": "Experience", "anchor": "Flexera", "item_index": 1},
                    "new_text": r"Built \textbf{Power Automate} workflows with \textbf{REST API} integrations.",
                    "reason": "Match the posting",
                }
            ],
        }
        response = self.client.post("/compile", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-test")
        self.assertIn("Resume_Example_Inc_Software_Engineer.pdf", response.headers["content-disposition"])
        self.assertIn("Built", compile_mock.call_args.args[0])
        events = (config.DATA_DIR / "applications.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(events), 1)

    @patch("backend.app.main.tracker.record_compiled", side_effect=OSError("disk full"))
    @patch("backend.app.main.compile_tex", return_value=CompileResult(pdf_bytes=b"%PDF-test"))
    def test_tracker_failure_does_not_break_compile(self, compile_mock, tracker_mock):
        response = self.client.post("/compile", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        tracker_mock.assert_called_once()

    @patch("backend.app.main.compile_tex", return_value=CompileResult(pdf_bytes=b"never"))
    def test_compile_rejects_unsafe_edit(self, compile_mock):
        payload = {
            "approved_edits": [
                {
                    "target": {"section": "Projects", "anchor": "Game Outcome Prediction Platform", "item_index": 0},
                    "new_text": r"Deployed \textbf{Docker} with \input{secret}.",
                    "reason": "unsafe",
                }
            ]
        }
        response = self.client.post("/compile", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        compile_mock.assert_not_called()

    @patch(
        "backend.app.main.compile_tex",
        return_value=CompileResult(pdf_bytes=b"%PDF-two-pages", page_count=2),
    )
    def test_compile_rejects_and_does_not_save_multi_page_pdf(self, compile_mock):
        response = self.client.post("/compile", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertIn("exactly one page is required", response.json()["detail"]["message"])
        self.assertEqual(list(config.OUTPUT_DIR.iterdir()), [])
        compile_mock.assert_called_once()

    @patch("backend.app.main.generate_edits")
    def test_generate_diff_marks_untraceable_proposal(self, generate_mock):
        generate_mock.return_value = [
            ProposedEdit(
                target=EditTarget(
                    section="Projects", anchor="Game Outcome Prediction Platform", item_index=0
                ),
                new_text=r"Deployed with \textbf{Docker}.",
                reason="posting asks for it",
            )
        ]
        payload = {
            "job_text": "Docker " * 30,
            "keywords": [
                {"term": "Docker", "category": "technology", "importance": "high", "evidence": "Required"}
            ],
        }
        response = self.client.post("/generate-diff", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["edits"][0]["traceable"])

    def test_tailor_job_runs_backend_pipeline_and_returns_polled_result(self):
        analysis = ExtractKeywordsResponse(
            company="Example Co",
            role="Software Engineer",
            keywords=[
                Keyword(
                    term="REST API",
                    category="technology",
                    importance="high",
                    evidence="Required experience",
                )
            ],
        )

        with (
            patch("backend.app.main.extract_keywords", return_value=analysis),
            patch("backend.app.main.generate_edits", return_value=[]),
            patch("backend.app.main.threading.Thread") as thread_class,
        ):
            def run_immediately():
                thread_args = thread_class.call_args.kwargs
                thread_args["target"](*thread_args["args"])

            thread_class.return_value.start.side_effect = run_immediately
            started = self.client.post(
                "/tailor/start",
                json={"job_text": "REST API software engineering role " * 5},
                headers=self.headers,
            )

        self.assertEqual(started.status_code, 200)
        job_id = started.json()["job_id"]
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        status = self.client.get(f"/tailor/status/{job_id}", headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "done")
        self.assertEqual(status.json()["company"], "Example Co")
        self.assertEqual(status.json()["role"], "Software Engineer")
        self.assertEqual(status.json()["keywords"][0]["term"], "REST API")
        self.assertEqual(status.json()["fit"]["score"], 100)

    def test_cover_letter_start_status_and_compile_confirmation_gate(self):
        draft = CoverLetterDraftResponse(
            paragraphs=[
                LetterParagraph(text="I am excited to apply for this role."),
                LetterParagraph(text="I bring Kubernetes experience to this work."),
            ]
        )
        keyword_payload = {
            "term": "Kubernetes",
            "category": "technology",
            "importance": "high",
            "evidence": "required",
        }
        with (
            patch("backend.app.main.draft_cover_letter", return_value=draft),
            patch("backend.app.main.threading.Thread") as thread_class,
        ):
            def run_immediately():
                thread_args = thread_class.call_args.kwargs
                thread_args["target"](*thread_args["args"])

            thread_class.return_value.start.side_effect = run_immediately
            started = self.client.post(
                "/cover-letter/start",
                json={
                    "job_text": "A sufficiently long Kubernetes engineering role. " * 3,
                    "company": "Acme",
                    "role": "Engineer",
                    "keywords": [keyword_payload],
                },
                headers=self.headers,
            )
        self.assertEqual(started.status_code, 200)
        status = self.client.get(
            f"/cover-letter/status/{started.json()['job_id']}", headers=self.headers
        )
        self.assertEqual(status.json()["status"], "done")
        self.assertTrue(status.json()["paragraphs"][1]["issues"])

        payload = {
            "company": "Acme",
            "role": "Engineer",
            "paragraphs": [{"text": "I bring Kubernetes experience to this work."}],
            "keywords": [keyword_payload],
        }
        blocked = self.client.post("/cover-letter/compile", json=payload, headers=self.headers)
        self.assertEqual(blocked.status_code, 422)
        with patch(
            "backend.app.main.compile_tex",
            return_value=CompileResult(pdf_bytes=b"%PDF-letter"),
        ) as compile_mock:
            payload["confirmed_by_user"] = True
            allowed = self.client.post("/cover-letter/compile", json=payload, headers=self.headers)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("CoverLetter_Acme_Engineer.pdf", allowed.headers["content-disposition"])
        self.assertEqual(compile_mock.call_args.kwargs["source_name"], "cover_letter.tex")

    def test_cover_letter_unsafe_draft_paragraph_is_flagged_not_fatal(self):
        draft = CoverLetterDraftResponse(
            paragraphs=[
                LetterParagraph(text="My models achieved 90% accuracy in production."),
                LetterParagraph(text=r"Unsafe \input{secret} paragraph."),
            ]
        )
        with (
            patch("backend.app.main.draft_cover_letter", return_value=draft),
            patch("backend.app.main.threading.Thread") as thread_class,
        ):
            def run_immediately():
                thread_args = thread_class.call_args.kwargs
                thread_args["target"](*thread_args["args"])

            thread_class.return_value.start.side_effect = run_immediately
            started = self.client.post(
                "/cover-letter/start",
                json={
                    "job_text": "A sufficiently long machine learning role posting. " * 3,
                    "company": "Acme",
                    "role": "Engineer",
                    "keywords": [
                        {
                            "term": "machine learning",
                            "category": "technology",
                            "importance": "high",
                            "evidence": "required",
                        }
                    ],
                },
                headers=self.headers,
            )
        status = self.client.get(
            f"/cover-letter/status/{started.json()['job_id']}", headers=self.headers
        )
        self.assertEqual(status.json()["status"], "done")
        paragraphs = status.json()["paragraphs"]
        self.assertEqual(paragraphs[0]["issues"], [])
        self.assertTrue(any(issue.startswith("unsafe:") for issue in paragraphs[1]["issues"]))

    @patch("backend.app.main.compile_tex", return_value=CompileResult(pdf_bytes=b"never"))
    def test_cover_letter_latex_safety_cannot_be_overridden(self, compile_mock):
        response = self.client.post(
            "/cover-letter/compile",
            json={
                "paragraphs": [{"text": "Unsafe \\input{secret}"}],
                "confirmed_by_user": True,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)
        compile_mock.assert_not_called()

    def test_tailor_thread_does_not_block_health_or_status_polling(self):
        extraction_started = threading.Event()
        release_extraction = threading.Event()
        analysis = ExtractKeywordsResponse(company="Example", role="Role", keywords=[])

        def slow_extract(_job_text):
            extraction_started.set()
            release_extraction.wait(timeout=5)
            return analysis

        with (
            patch("backend.app.main.extract_keywords", side_effect=slow_extract),
            patch("backend.app.main.generate_edits", return_value=[]),
        ):
            started = self.client.post(
                "/tailor/start",
                json={"job_text": "A sufficiently long software engineering posting. " * 3},
                headers=self.headers,
            )
            self.assertEqual(started.status_code, 200)
            self.assertTrue(extraction_started.wait(timeout=1))
            job_id = started.json()["job_id"]
            self.assertEqual(self.client.get("/health").status_code, 200)
            running = self.client.get(f"/tailor/status/{job_id}", headers=self.headers)
            self.assertEqual(running.json()["status"], "running")
            release_extraction.set()

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status = self.client.get(f"/tailor/status/{job_id}", headers=self.headers)
                if status.json()["status"] == "done":
                    break
                time.sleep(0.01)
            self.assertEqual(status.json()["status"], "done")

    def test_tailor_status_rejects_unknown_job(self):
        response = self.client.get("/tailor/status/not-a-job", headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "unknown job_id")


if __name__ == "__main__":
    unittest.main()
