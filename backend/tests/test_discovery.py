import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from pydantic import ValidationError

from backend.app import config, discovery
from backend.app.models import ExtractKeywordsResponse, Keyword


ENCODED_CONTENT = (
    "&lt;div class=&quot;content-intro&quot;&gt;"
    "&lt;p&gt;Build APIs &amp; tools.&lt;/p&gt;&lt;/div&gt;"
)


def greenhouse_job(
    job_id: int,
    title: str = "Software Engineering Intern",
    *,
    content: str = ENCODED_CONTENT,
    url: str | None = None,
) -> dict:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": "Remote"},
        "absolute_url": url or f"https://boards.greenhouse.io/example/jobs/{job_id}",
        "updated_at": "2026-08-01T12:00:00-07:00",
        "content": content,
    }


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.previous_data_dir = config.DATA_DIR
        self.previous_output_dir = config.OUTPUT_DIR
        self.previous_companies_path = config.COMPANIES_PATH
        self.previous_resume_path = config.RESUME_TEX_PATH
        self.tempdir = tempfile.TemporaryDirectory(prefix="resume-discovery-tests-")
        root = Path(self.tempdir.name)
        config.DATA_DIR = root / "data"
        config.OUTPUT_DIR = root / "output"
        config.COMPANIES_PATH = root / "companies.json"
        config.RESUME_TEX_PATH = root / "resume.tex"
        config.RESUME_TEX_PATH.write_text("Experienced with Python.", encoding="utf-8")

    def tearDown(self):
        config.DATA_DIR = self.previous_data_dir
        config.OUTPUT_DIR = self.previous_output_dir
        config.COMPANIES_PATH = self.previous_companies_path
        config.RESUME_TEX_PATH = self.previous_resume_path
        self.tempdir.cleanup()

    def write_companies(self, companies: list[dict], keywords: list[str] | None = None) -> None:
        config.COMPANIES_PATH.write_text(
            json.dumps(
                {
                    "title_keywords": [] if keywords is None else keywords,
                    "companies": companies,
                }
            ),
            encoding="utf-8",
        )

    def record(self, posting_id: str = "example:101", **overrides) -> None:
        fields = {
            "id": posting_id,
            "company": "Example Co",
            "role": "Software Engineering Intern",
            "location": "Remote",
            "url": "https://example.com/jobs/101",
            "platform": "greenhouse",
            "description": "Python API work",
        }
        fields.update(overrides)
        discovery.record_seen(**fields)

    def test_load_companies_config_parses_valid_file(self):
        self.write_companies(
            [{"name": "Example Co", "platform": "greenhouse", "slug": "example-co"}],
            ["intern", "new grad"],
        )
        loaded = discovery.load_companies_config()
        self.assertEqual(loaded.title_keywords, ["intern", "new grad"])
        self.assertEqual(loaded.companies[0].slug, "example-co")

    def test_load_companies_config_rejects_unknown_platform(self):
        self.write_companies([{"name": "Example Co", "platform": "workable", "slug": "example"}])
        with self.assertRaisesRegex(ValidationError, "platform"):
            discovery.load_companies_config()

    def test_load_companies_config_returns_empty_default_when_missing(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            loaded = discovery.load_companies_config()
        self.assertEqual(loaded.companies, [])
        self.assertEqual(loaded.title_keywords, [])
        self.assertIn("no companies.json found", stderr.getvalue())

    @patch("backend.app.discovery.httpx.Client")
    def test_fetch_greenhouse_jobs_uses_expected_endpoint_and_shape(self, client_class):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"jobs": [greenhouse_job(101)]}
        client = client_class.return_value.__enter__.return_value
        client.get.return_value = response

        jobs = discovery.fetch_greenhouse_jobs("example")

        self.assertEqual(jobs[0]["id"], 101)
        client.get.assert_called_once_with(
            "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"
        )
        client_class.assert_called_once_with(timeout=10.0)

    @patch("backend.app.discovery.httpx.Client")
    def test_fetch_lever_jobs_uses_expected_endpoint_and_shape(self, client_class):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [{"id": "lever-101"}]
        client = client_class.return_value.__enter__.return_value
        client.get.return_value = response

        jobs = discovery.fetch_lever_jobs("example")

        self.assertEqual(jobs[0]["id"], "lever-101")
        client.get.assert_called_once_with(
            "https://api.lever.co/v0/postings/example?mode=json"
        )
        response.json.return_value = {"postings": []}
        with self.assertRaisesRegex(ValueError, "not a list"):
            discovery.fetch_lever_jobs("example")

    @patch("backend.app.discovery.httpx.Client")
    def test_fetch_ashby_jobs_uses_expected_endpoint_and_shape(self, client_class):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"jobs": [{"id": "ashby-101"}]}
        client = client_class.return_value.__enter__.return_value
        client.get.return_value = response

        jobs = discovery.fetch_ashby_jobs("example")

        self.assertEqual(jobs[0]["id"], "ashby-101")
        client.get.assert_called_once_with(
            "https://api.ashbyhq.com/posting-api/job-board/example"
        )
        response.json.return_value = {"results": []}
        with self.assertRaisesRegex(ValueError, "jobs array"):
            discovery.fetch_ashby_jobs("example")

    def test_normalize_lever_job_uses_plain_description(self):
        normalized = discovery._normalize_lever_job(
            {
                "id": "lever-101",
                "text": " Software Engineer ",
                "hostedUrl": "https://jobs.lever.co/example/lever-101",
                "categories": {"location": "Remote"},
                "descriptionPlain": "Build APIs.\n\nWork with Python.",
            }
        )

        self.assertEqual(normalized["title"], "Software Engineer")
        self.assertEqual(normalized["location"], "Remote")
        self.assertEqual(normalized["description"], "Build APIs. Work with Python.")

    def test_normalize_ashby_job_strips_real_world_title_whitespace(self):
        normalized = discovery._normalize_ashby_job(
            {
                "id": "ashby-101",
                "title": " Security Engineer, Cloud",
                "jobUrl": "https://jobs.ashbyhq.com/example/ashby-101",
                "location": "New York",
                "descriptionPlain": "Secure cloud infrastructure.",
            }
        )

        self.assertEqual(normalized["title"], "Security Engineer, Cloud")
        self.assertEqual(normalized["location"], "New York")
        self.assertEqual(normalized["description"], "Secure cloud infrastructure.")

    def test_strip_html_unescapes_before_removing_tags_and_caps_length(self):
        self.assertEqual(discovery._strip_html(ENCODED_CONTENT), "Build APIs & tools.")
        with patch.object(config, "MAX_JOB_TEXT_CHARS", 5):
            self.assertEqual(discovery._strip_html(ENCODED_CONTENT), "Build")

    @patch("backend.app.discovery.time.sleep")
    @patch("backend.app.discovery.httpx.Client")
    def test_poll_continues_after_non_200_company(self, client_class, sleep):
        self.write_companies(
            [
                {"name": "Missing Co", "platform": "greenhouse", "slug": "missing"},
                {"name": "Working Co", "platform": "greenhouse", "slug": "working"},
            ],
            ["intern"],
        )
        bad_request = httpx.Request(
            "GET", "https://boards-api.greenhouse.io/v1/boards/missing/jobs?content=true"
        )
        bad_response = httpx.Response(404, request=bad_request)
        good_response = Mock()
        good_response.raise_for_status.return_value = None
        good_response.json.return_value = {"jobs": [greenhouse_job(202)]}
        client = client_class.return_value.__enter__.return_value
        client.get.side_effect = [bad_response, good_response]

        stderr = StringIO()
        with redirect_stderr(stderr):
            new = discovery.poll()

        self.assertEqual([posting["id"] for posting in new], ["working:202"])
        self.assertIn("could not poll Missing Co", stderr.getvalue())
        sleep.assert_called_once_with(discovery._POLL_DELAY_SECONDS)

    @patch("backend.app.discovery.time.sleep")
    @patch("backend.app.discovery.fetch_greenhouse_jobs")
    def test_poll_filters_titles_and_does_not_duplicate_seen_events(self, fetch, _sleep):
        self.write_companies(
            [{"name": "Example Co", "platform": "greenhouse", "slug": "example"}],
            ["intern", "new grad"],
        )
        fetch.return_value = [
            greenhouse_job(101),
            greenhouse_job(102, "Senior Software Engineer"),
        ]

        self.assertEqual(len(discovery.poll()), 1)
        self.assertEqual(discovery.poll(), [])
        lines = (config.DATA_DIR / "discovered_jobs.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(discovery.read_postings()[0]["description"], "Build APIs & tools.")

    @patch("backend.app.discovery.time.sleep")
    @patch("backend.app.discovery.fetch_ashby_jobs")
    @patch("backend.app.discovery.fetch_lever_jobs")
    @patch("backend.app.discovery.fetch_greenhouse_jobs")
    def test_poll_mixes_all_supported_platforms(
        self, fetch_greenhouse, fetch_lever, fetch_ashby, sleep
    ):
        self.write_companies(
            [
                {"name": "Green Co", "platform": "greenhouse", "slug": "green"},
                {"name": "Lever Co", "platform": "lever", "slug": "lever"},
                {"name": "Ashby Co", "platform": "ashby", "slug": "ashby"},
            ],
            ["engineer"],
        )
        fetch_greenhouse.return_value = [greenhouse_job(101, "Software Engineer")]
        fetch_lever.return_value = [
            {
                "id": "lever-202",
                "text": "Platform Engineer",
                "hostedUrl": "https://jobs.lever.co/lever/lever-202",
                "categories": {"location": "Remote"},
                "descriptionPlain": "Lever plain text",
            }
        ]
        fetch_ashby.return_value = [
            {
                "id": "ashby-303",
                "title": " Infrastructure Engineer ",
                "jobUrl": "https://jobs.ashbyhq.com/ashby/ashby-303",
                "location": "Chicago",
                "descriptionPlain": "Ashby plain text",
            }
        ]

        postings = discovery.poll()

        self.assertEqual(
            [posting["platform"] for posting in postings],
            ["greenhouse", "lever", "ashby"],
        )
        self.assertEqual(
            [posting["description"] for posting in postings],
            ["Build APIs & tools.", "Lever plain text", "Ashby plain text"],
        )
        self.assertEqual(postings[2]["role"], "Infrastructure Engineer")
        self.assertEqual(sleep.call_count, 2)

    @patch("backend.app.discovery.time.sleep")
    @patch("backend.app.discovery.fetch_greenhouse_jobs")
    def test_title_filter_is_whole_word_not_substring(self, fetch, _sleep):
        # Regression: "intern" as a plain substring also matches "Internal"/
        # "International" (both start with the literal characters "intern"),
        # which flooded results with unrelated senior/business roles when
        # verified against real Greenhouse postings while writing this spec.
        self.write_companies(
            [{"name": "Example Co", "platform": "greenhouse", "slug": "example"}],
            ["intern"],
        )
        fetch.return_value = [
            greenhouse_job(101, "Senior Internal Auditor"),
            greenhouse_job(102, "Software Engineering Intern"),
        ]
        new = discovery.poll()
        self.assertEqual([posting["role"] for posting in new], ["Software Engineering Intern"])

    @patch("backend.app.discovery.time.sleep")
    @patch("backend.app.discovery.fetch_greenhouse_jobs")
    def test_empty_title_keywords_records_every_role(self, fetch, _sleep):
        self.write_companies(
            [{"name": "Example Co", "platform": "greenhouse", "slug": "example"}]
        )
        fetch.return_value = [greenhouse_job(101, "Staff Software Engineer")]
        [posting] = discovery.poll()
        self.assertEqual(posting["role"], "Staff Software Engineer")

    def test_status_fit_and_malformed_line_fold_correctly(self):
        self.record()
        discovery.record_status("example:101", "dismissed")
        discovery.record_status("example:101", "tailored")
        discovery.record_fit(
            "example:101", fit_score=78, keywords_total=12, keywords_matched=9
        )
        with (config.DATA_DIR / "discovered_jobs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("not-json\n")

        stderr = StringIO()
        with redirect_stderr(stderr):
            [posting] = discovery.read_postings()
        self.assertEqual(posting["status"], "tailored")
        self.assertEqual(posting["fit_score"], 78)
        self.assertEqual(posting["keywords_matched"], 9)
        self.assertIn("skipped malformed discovery line", stderr.getvalue())

    @patch("backend.app.discovery.extract_keywords")
    def test_score_new_only_scores_postings_without_a_fit_event(self, extract):
        self.record("example:101")
        self.record("example:102")
        discovery.record_fit("example:102", fit_score=20, keywords_total=1, keywords_matched=0)
        extract.return_value = ExtractKeywordsResponse(
            company="Example Co",
            role="Intern",
            keywords=[
                Keyword(
                    term="Python",
                    category="technology",
                    importance="high",
                    evidence="required",
                )
            ],
        )

        [scored] = discovery.score_new()

        self.assertEqual(scored["id"], "example:101")
        self.assertEqual(scored["fit_score"], 100)
        self.assertEqual(scored["keywords_matched"], 1)
        extract.assert_called_once_with("Python API work")

    @patch("backend.app.discovery.extract_keywords")
    def test_score_new_skips_a_failing_posting_and_continues(self, extract):
        self.record("example:101")
        self.record("example:102")
        extract.side_effect = [
            RuntimeError("model unavailable"),
            ExtractKeywordsResponse(
                company="Example Co",
                role="Intern",
                keywords=[
                    Keyword(
                        term="Python",
                        category="technology",
                        importance="high",
                        evidence="required",
                    )
                ],
            ),
        ]
        stderr = StringIO()
        with redirect_stderr(stderr):
            scored = discovery.score_new()
        self.assertEqual([item["id"] for item in scored], ["example:102"])
        self.assertIn("could not score example:101", stderr.getvalue())

    def test_unique_prefix_status_and_ambiguous_prefix(self):
        self.record("example:101")
        self.record("example:102")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            discovery._resolve_posting("example:10", discovery.read_postings())
        stdout = StringIO()
        with redirect_stdout(stdout):
            result = discovery.main(["dismiss", "example:101"])
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "example:101")
        self.assertEqual(discovery.read_postings()[0]["status"], "dismissed")

    def test_generate_report_sorts_scores_and_escapes_cells_and_url(self):
        with patch(
            "backend.app.discovery._timestamp",
            side_effect=["2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00"],
        ):
            self.record(
                "unsafe:1",
                company="<script>alert(1)</script>",
                role="<script>role()</script>",
                url="https://example.com/<script>url()</script>",
            )
            self.record("safe:2", company="Safe Co", role="Newer role")
        discovery.record_fit("unsafe:1", fit_score=90, keywords_total=1, keywords_matched=1)
        discovery.record_fit("safe:2", fit_score=50, keywords_total=1, keywords_matched=1)

        destination = discovery.generate_report()
        report = destination.read_text(encoding="utf-8")

        self.assertLess(report.index("&lt;script&gt;role()"), report.index("Newer role"))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", report)
        self.assertIn('href="https://example.com/&lt;script&gt;url()&lt;/script&gt;"', report)
        self.assertNotIn("<script>", report)

    def test_list_filters_status_and_minimum_fit(self):
        self.record("example:101", role="High fit")
        self.record("example:102", role="Unscored")
        discovery.record_fit("example:101", fit_score=80, keywords_total=1, keywords_matched=1)
        discovery.record_status("example:101", "tailored")

        stdout = StringIO()
        with redirect_stdout(stdout):
            discovery._list_postings(new_only=True)
        self.assertNotIn("High fit", stdout.getvalue())
        self.assertIn("Unscored", stdout.getvalue())

        stdout = StringIO()
        with redirect_stdout(stdout):
            discovery._list_postings(min_fit=75)
        self.assertIn("High fit", stdout.getvalue())
        self.assertNotIn("Unscored", stdout.getvalue())

    def test_console_streams_are_configured_for_unicode_output(self):
        stdout = Mock()
        stderr = Mock()
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            discovery._configure_console_encoding()
        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="backslashreplace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="backslashreplace")


if __name__ == "__main__":
    unittest.main()
