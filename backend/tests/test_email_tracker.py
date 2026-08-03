import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import config, email_tracker, tracker


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def gmail_message(sender: str, subject: str, body: str, *, html_body: bool = False) -> dict:
    mime_type = "text/html" if html_body else "text/plain"
    return {
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "parts": [
                {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {"mimeType": mime_type, "body": {"data": encoded(body)}}
                    ],
                }
            ],
        }
    }


class EmailTrackerTests(unittest.TestCase):
    def setUp(self):
        self.previous_data_dir = config.DATA_DIR
        self.previous_client_secret_path = config.GMAIL_CLIENT_SECRET_PATH
        self.previous_token_path = config.GMAIL_TOKEN_PATH
        self.tempdir = tempfile.TemporaryDirectory(prefix="resume-email-tracker-tests-")
        root = Path(self.tempdir.name)
        config.DATA_DIR = root / "data"
        config.GMAIL_CLIENT_SECRET_PATH = root / "gmail-client.json"
        config.GMAIL_TOKEN_PATH = root / "gmail-token.json"

    def tearDown(self):
        config.DATA_DIR = self.previous_data_dir
        config.GMAIL_CLIENT_SECRET_PATH = self.previous_client_secret_path
        config.GMAIL_TOKEN_PATH = self.previous_token_path
        self.tempdir.cleanup()

    def record_application(self, company: str = "Acme", role: str = "Engineer") -> str:
        return tracker.record_compiled(
            company=company,
            role=role,
            filename="Resume.pdf",
            edits_applied=0,
            fit_score=80,
            keywords_total=10,
            keywords_matched=8,
        )

    def test_find_matching_application_uses_company_and_most_recent_date(self):
        applications = [
            {
                "event": "compiled",
                "id": "older",
                "at": "2026-01-01T00:00:00+00:00",
                "company": "Acme",
            },
            {
                "event": "compiled",
                "id": "newer",
                "at": "2026-02-01T00:00:00+00:00",
                "company": "Acme",
            },
            {
                "event": "letter",
                "id": "letter",
                "at": "2026-03-01T00:00:00+00:00",
                "company": "Acme",
            },
        ]

        matched = email_tracker._find_matching_application(
            "recruiting@example.com", "Acme update", "", applications
        )

        self.assertEqual(matched["id"], "newer")
        self.assertIsNone(
            email_tracker._find_matching_application(
                "recruiting@example.com", "Different company", "", applications
            )
        )

    def test_company_match_uses_boundaries_instead_of_substrings(self):
        applications = [
            {
                "event": "compiled",
                "id": "meta-1",
                "at": "2026-01-01T00:00:00+00:00",
                "company": "Meta",
            }
        ]
        self.assertIsNone(
            email_tracker._find_matching_application(
                "newsletter@example.com", "Metadata engineering digest", "", applications
            )
        )
        self.assertEqual(
            email_tracker._find_matching_application(
                "jobs@meta.com", "META application update", "", applications
            )["id"],
            "meta-1",
        )

    def test_keyword_classifiers_do_not_call_local_model(self):
        examples = {
            "offer": "We are pleased to offer you this role.",
            "interview": "Please schedule an interview with our team.",
            "rejected": "Unfortunately, we are pursuing other candidates.",
            "screen": "Let's arrange an initial screening next week.",
        }
        with patch("backend.app.email_tracker.llm.classify_application_email") as classify:
            for expected, body in examples.items():
                with self.subTest(expected=expected):
                    status, evidence = email_tracker._classify_email("Application update", body)
                    self.assertEqual(status, expected)
                    self.assertIn("Keyword match", evidence)
            classify.assert_not_called()

    def test_local_model_fallback_result_is_used_and_unrelated_is_ignored(self):
        with patch(
            "backend.app.email_tracker.llm.classify_application_email",
            return_value="interview",
        ) as classify:
            status, evidence = email_tracker._classify_email(
                "A quick update", "We would like to continue the process."
            )
        self.assertEqual(status, "interview")
        self.assertIn("Local model", evidence)
        classify.assert_called_once()

        for unrelated in (None, "unrelated"):
            with patch(
                "backend.app.email_tracker.llm.classify_application_email",
                return_value=unrelated,
            ):
                self.assertEqual(
                    email_tracker._classify_email("Newsletter", "Company news"),
                    (None, ""),
                )

    def test_extract_message_prefers_plain_text_and_falls_back_to_html(self):
        plain = email_tracker._extract_message(
            gmail_message("jobs@acme.com", "Update", "Plain body")
        )
        html_message = gmail_message(
            "jobs@acme.com", "Update", "<p>HTML &amp; body</p>", html_body=True
        )
        fallback = email_tracker._extract_message(html_message)

        self.assertEqual(plain["body"], "Plain body")
        self.assertEqual(fallback["body"], "HTML & body")

    def test_message_body_is_capped_before_classification(self):
        with patch.object(config, "MAX_JOB_TEXT_CHARS", 5):
            extracted = email_tracker._extract_message(
                gmail_message("jobs@acme.com", "Update", "123456789")
            )
        self.assertEqual(extracted["body"], "12345")

    def test_message_listing_follows_pagination(self):
        service = Mock()
        messages = service.users.return_value.messages.return_value
        first = Mock()
        first.execute.return_value = {
            "messages": [{"id": "first"}],
            "nextPageToken": "next-page",
        }
        second = Mock()
        second.execute.return_value = {"messages": [{"id": "second"}]}
        messages.list.side_effect = [first, second]

        message_ids = email_tracker._list_message_ids(service)

        self.assertEqual(message_ids, ["first", "second"])
        self.assertEqual(
            messages.list.call_args_list[0].kwargs,
            {"userId": "me", "q": "newer_than:180d"},
        )
        self.assertEqual(
            messages.list.call_args_list[1].kwargs,
            {"userId": "me", "q": "newer_than:180d", "pageToken": "next-page"},
        )

    def test_missing_oauth_files_fail_before_any_external_call(self):
        with self.assertRaisesRegex(ValueError, "authorize.*first"):
            email_tracker._load_credentials()
        with (
            patch("backend.app.email_tracker.InstalledAppFlow") as flow,
            self.assertRaisesRegex(ValueError, "follow README"),
        ):
            email_tracker.authorize()
        flow.from_client_secrets_file.assert_not_called()

    def test_authorize_requests_only_readonly_scope_and_writes_token(self):
        config.GMAIL_CLIENT_SECRET_PATH.write_text("{}", encoding="utf-8")
        credentials = Mock()
        credentials.to_json.return_value = '{"refresh_token":"local-test"}'
        flow = Mock()
        flow.run_local_server.return_value = credentials
        with patch(
            "backend.app.email_tracker.InstalledAppFlow.from_client_secrets_file",
            return_value=flow,
        ) as create_flow:
            destination = email_tracker.authorize()

        self.assertEqual(destination, config.GMAIL_TOKEN_PATH)
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            '{"refresh_token":"local-test"}',
        )
        create_flow.assert_called_once_with(
            str(config.GMAIL_CLIENT_SECRET_PATH),
            scopes=[email_tracker.GMAIL_READONLY_SCOPE],
        )
        flow.run_local_server.assert_called_once_with(port=0)

    def test_expired_credentials_refresh_and_persist(self):
        config.GMAIL_TOKEN_PATH.write_text("{}", encoding="utf-8")
        credentials = Mock(
            expired=True,
            refresh_token="refresh-token",
            valid=True,
        )
        credentials.to_json.return_value = '{"refreshed":true}'
        request = Mock()
        with (
            patch(
                "backend.app.email_tracker.Credentials.from_authorized_user_file",
                return_value=credentials,
            ) as load,
            patch("backend.app.email_tracker.Request", return_value=request),
        ):
            loaded = email_tracker._load_credentials()

        self.assertIs(loaded, credentials)
        load.assert_called_once_with(
            str(config.GMAIL_TOKEN_PATH),
            scopes=[email_tracker.GMAIL_READONLY_SCOPE],
        )
        credentials.refresh.assert_called_once_with(request)
        self.assertEqual(
            config.GMAIL_TOKEN_PATH.read_text(encoding="utf-8"),
            '{"refreshed":true}',
        )

    def test_gmail_scope_is_read_only(self):
        self.assertEqual(
            email_tracker.GMAIL_READONLY_SCOPE,
            "https://www.googleapis.com/auth/gmail.readonly",
        )

    def test_poll_stages_matched_and_unmatched_and_deduplicates_seen_messages(self):
        application_id = self.record_application()
        service = Mock()
        messages = service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {
            "messages": [{"id": "msg-1"}, {"id": "msg-2"}, {"id": "msg-3"}]
        }
        requests = []
        for message in (
            gmail_message(
                "jobs@acme.com",
                "Acme application update",
                "We are pleased to offer you the role.",
            ),
            gmail_message(
                "jobs@different.com",
                "Different Co application update",
                "Unfortunately, we are not moving forward.",
            ),
            gmail_message("news@example.com", "Newsletter", "This month's company news."),
        ):
            request = Mock()
            request.execute.return_value = message
            requests.append(request)
        messages.get.side_effect = requests

        with (
            patch("backend.app.email_tracker._gmail_service", return_value=service),
            patch(
                "backend.app.email_tracker.llm.classify_application_email",
                return_value=None,
            ) as classify,
        ):
            processed = email_tracker.poll()
            processed_again = email_tracker.poll()

        self.assertEqual(
            [event["event"] for event in processed],
            ["suggested", "unmatched", "seen"],
        )
        self.assertEqual(processed_again, [])
        self.assertEqual(messages.get.call_count, 3)
        classify.assert_called_once()
        [suggestion] = email_tracker.pending_suggestions()
        self.assertEqual(suggestion["application_id"], application_id)
        self.assertEqual(suggestion["status"], "offer")
        events = email_tracker._read_events()
        self.assertEqual(sum(event["event"] == "seen" for event in events), 3)
        self.assertEqual(sum(event["event"] == "unmatched" for event in events), 1)
        self.assertTrue(all("body" not in event for event in events))
        serialized = (config.DATA_DIR / "email_suggestions.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("We are pleased to offer you the role", serialized)
        self.assertNotIn("company news", serialized)

    def test_classifier_failure_does_not_mark_message_seen_so_poll_can_retry(self):
        service = Mock()
        messages = service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {"messages": [{"id": "retry-me"}]}
        request = Mock()
        request.execute.return_value = gmail_message(
            "jobs@acme.com", "Ambiguous update", "We have an update for you."
        )
        messages.get.return_value = request
        with (
            patch("backend.app.email_tracker._gmail_service", return_value=service),
            patch(
                "backend.app.email_tracker.llm.classify_application_email",
                side_effect=RuntimeError("model offline"),
            ),
            self.assertRaisesRegex(RuntimeError, "model offline"),
        ):
            email_tracker.poll()
        self.assertEqual(email_tracker._read_events(), [])

    def test_confirm_and_dismiss_are_review_gated(self):
        application_id = self.record_application()
        application = tracker.read_applications()[0]
        email_tracker._record_suggested(
            "abc111",
            application=application,
            status="interview",
            evidence="Keyword match",
            sender="jobs@acme.com",
            subject="Acme update",
        )
        with patch("backend.app.email_tracker.tracker.record_outcome") as record_outcome:
            confirmed_id = email_tracker.confirm_suggestion("abc1")
        self.assertEqual(confirmed_id, "abc111")
        record_outcome.assert_called_once_with(application_id, "interview")
        self.assertEqual(email_tracker.pending_suggestions(), [])

        email_tracker._record_suggested(
            "def222",
            application=application,
            status="rejected",
            evidence="Keyword match",
            sender="jobs@acme.com",
            subject="Acme update",
        )
        with patch("backend.app.email_tracker.tracker.record_outcome") as record_outcome:
            dismissed_id = email_tracker.dismiss_suggestion("def2")
        self.assertEqual(dismissed_id, "def222")
        record_outcome.assert_not_called()
        self.assertEqual(email_tracker.pending_suggestions(), [])

    def test_confirm_persists_real_tracker_outcome(self):
        application_id = self.record_application()
        application = tracker.read_applications()[0]
        email_tracker._record_suggested(
            "persist123",
            application=application,
            status="screen",
            evidence="Keyword match",
            sender="jobs@acme.com",
            subject="Acme update",
        )

        email_tracker.confirm_suggestion("persist")

        [updated] = tracker.read_applications()
        self.assertEqual(updated["id"], application_id)
        self.assertEqual(updated["status"], "screen")

    def test_malformed_suggestion_log_line_is_skipped(self):
        email_tracker._record_seen("valid")
        with (config.DATA_DIR / "email_suggestions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("not-json\n")
        with patch("sys.stderr"):
            events = email_tracker._read_events()
        self.assertEqual([event["id"] for event in events], ["valid"])

    def test_terminal_suggestion_cannot_be_reopened_by_duplicate_poll_event(self):
        self.record_application()
        application = tracker.read_applications()[0]
        email_tracker._record_suggested(
            "duplicate1",
            application=application,
            status="screen",
            evidence="First",
            sender="jobs@acme.com",
            subject="Update",
        )
        email_tracker.dismiss_suggestion("duplicate")
        email_tracker._record_suggested(
            "duplicate1",
            application=application,
            status="interview",
            evidence="Duplicate after terminal event",
            sender="jobs@acme.com",
            subject="Update",
        )
        self.assertEqual(email_tracker.pending_suggestions(), [])

    def test_ambiguous_suggestion_prefix_is_rejected(self):
        self.record_application()
        application = tracker.read_applications()[0]
        for suggestion_id in ("abc111", "abc222"):
            email_tracker._record_suggested(
                suggestion_id,
                application=application,
                status="screen",
                evidence="Keyword match",
                sender="jobs@acme.com",
                subject="Acme update",
            )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            email_tracker.confirm_suggestion("abc")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            email_tracker.dismiss_suggestion("abc")


if __name__ == "__main__":
    unittest.main()
