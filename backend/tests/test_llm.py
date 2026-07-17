import json
import unittest
from unittest.mock import Mock, patch

import httpx

from backend.app import config
from backend.app.llm import (
    LLMError,
    _call,
    _model_resume_context,
    _prepare_bullet_edits,
    _technology_reorders,
)
from backend.app.models import EditTarget, ExtractKeywordsResponse, Keyword, ProposedEdit
from backend.app.resume_parser import locate_target


class OllamaClientTests(unittest.TestCase):
    @patch("backend.app.llm.httpx.Client")
    def test_ollama_request_uses_schema_and_local_model(self, client_class):
        response = Mock()
        response.json.return_value = {
            "message": {"content": json.dumps({"company": "Acme", "role": "Intern", "keywords": []})}
        }
        response.raise_for_status.return_value = None
        client = client_class.return_value.__enter__.return_value
        client.post.return_value = response

        result = _call(system="system", prompt="prompt", response_model=ExtractKeywordsResponse)

        self.assertEqual(result.company, "Acme")
        url, = client.post.call_args.args
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(url, f"{config.OLLAMA_HOST}/api/chat")
        self.assertEqual(payload["model"], config.OLLAMA_MODEL)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(payload["options"]["num_predict"], 2048)
        self.assertEqual(payload["format"], ExtractKeywordsResponse.model_json_schema())

    @patch("backend.app.llm.httpx.Client")
    def test_connection_error_has_actionable_message(self, client_class):
        request = httpx.Request("POST", f"{config.OLLAMA_HOST}/api/chat")
        client = client_class.return_value.__enter__.return_value
        client.post.side_effect = httpx.ConnectError("offline", request=request)
        with self.assertRaisesRegex(LLMError, "Start Ollama"):
            _call(system="system", prompt="prompt", response_model=ExtractKeywordsResponse)

    @patch("backend.app.llm.httpx.Client")
    def test_custom_wire_schema_is_sent_but_pydantic_still_validates(self, client_class):
        response = Mock()
        response.json.return_value = {
            "message": {"content": json.dumps({"company": "Acme", "role": "Intern", "keywords": []})}
        }
        response.raise_for_status.return_value = None
        client = client_class.return_value.__enter__.return_value
        client.post.return_value = response
        wire_schema = {"type": "object"}

        result = _call(
            system="system",
            prompt="prompt",
            response_model=ExtractKeywordsResponse,
            format_schema=wire_schema,
        )

        self.assertEqual(result.role, "Intern")
        self.assertEqual(client.post.call_args.kwargs["json"]["format"], wire_schema)

    def test_rejects_nonlocal_ollama_host(self):
        previous = config.OLLAMA_HOST
        config.OLLAMA_HOST = "https://example.com"
        try:
            with self.assertRaisesRegex(LLMError, "local HTTP address"):
                _call(system="system", prompt="prompt", response_model=ExtractKeywordsResponse)
        finally:
            config.OLLAMA_HOST = previous

    def test_technology_reordering_is_deterministic_and_membership_safe(self):
        resume = config.RESUME_TEX_PATH.read_text(encoding="utf-8")
        keywords = [
            Keyword(term="SQL", category="technology", importance="high", evidence="required"),
            Keyword(term="Python", category="technology", importance="high", evidence="required"),
        ]
        edits = _technology_reorders(keywords, resume)
        languages = next(edit for edit in edits if edit.target.anchor == "Languages")
        self.assertTrue(languages.new_text.startswith("Python, SQL"))
        original = locate_target(resume, languages.target).text
        self.assertCountEqual(languages.new_text.split(", "), original.split(", "))

    def test_technology_reordering_matches_required_phrase_categories(self):
        resume = config.RESUME_TEX_PATH.read_text(encoding="utf-8")
        keywords = [
            Keyword(
                term="Python and SQL data pipelines",
                category="required",
                importance="high",
                evidence="required",
            )
        ]
        edits = _technology_reorders(keywords, resume)
        languages = next(edit for edit in edits if edit.target.anchor == "Languages")
        self.assertTrue(languages.new_text.startswith("Python, SQL"))

    def test_plain_bullet_preparation_escapes_specials_and_drops_latex(self):
        target = EditTarget(section="Projects", anchor="Game Outcome Prediction Platform", item_index=0)
        safe = ProposedEdit(target=target, new_text="Improved accuracy by 90% with R&D_data.", reason="test")
        malformed = ProposedEdit(target=target, new_text="Used \\textbf{Python}.", reason="test")
        prepared = _prepare_bullet_edits([safe, malformed])
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].new_text, r"Improved accuracy by 90\% with R\&D\_data.")

    def test_model_resume_context_is_addressed_plain_text(self):
        resume = config.RESUME_TEX_PATH.read_text(encoding="utf-8")
        context = _model_resume_context(resume)
        self.assertIn("SECTION: Experience", context)
        self.assertIn("SUBSECTION: Flexera", context)
        self.assertIn("BULLET 0:", context)
        self.assertNotIn(r"\textbf", context)
        self.assertNotIn(r"\textmd", context)


if __name__ == "__main__":
    unittest.main()
