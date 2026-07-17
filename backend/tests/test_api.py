import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import config
from backend.app.latex_compile import CompileResult
from backend.app.main import app
from backend.app.models import EditTarget, ProposedEdit


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_secret = config.SHARED_SECRET
        self.previous_output_dir = config.OUTPUT_DIR
        self.output_tempdir = tempfile.TemporaryDirectory(prefix="resume-tailor-tests-")
        config.SHARED_SECRET = "test-secret"
        config.OUTPUT_DIR = Path(self.output_tempdir.name)
        self.client = TestClient(app)
        self.headers = {"X-Extension-Secret": "test-secret"}

    def tearDown(self):
        config.SHARED_SECRET = self.previous_secret
        config.OUTPUT_DIR = self.previous_output_dir
        self.output_tempdir.cleanup()

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


if __name__ == "__main__":
    unittest.main()
