import argparse
import base64
import html
import json
import re
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import config, llm, tracker


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_QUERY = "newer_than:180d"
_EMAIL_OUTCOMES = {"applied", "screen", "interview", "offer", "rejected"}
_KEYWORD_RULES: list[tuple[str, re.Pattern]] = [
    (
        "offer",
        re.compile(r"\b(pleased to offer|extend(?:ing)? an offer|offer letter)\b", re.I),
    ),
    (
        "interview",
        re.compile(
            r"\b(schedule (?:a|an) (?:call|interview)|interview (?:invit|request)|"
            r"next steps? in (?:the|our) (?:interview )?process)\b",
            re.I,
        ),
    ),
    (
        "rejected",
        re.compile(
            r"\b(unfortunately|regret to inform|not moving forward|pursuing other candidates|"
            r"decided not to move forward)\b",
            re.I,
        ),
    ),
    (
        "screen",
        re.compile(
            r"\b(phone screen|recruiter (?:screen|call)|initial (?:screening|call))\b",
            re.I,
        ),
    ),
    (
        "applied",
        re.compile(
            r"\b(thank you for applying|thanks for applying|(?:we|we've|we have) received your application|"
            r"your application (?:has been|was) (?:received|submitted)|thank you for your interest in)\b",
            re.I,
        ),
    ),
]
_append_lock = threading.Lock()


def _events_path() -> Path:
    return config.DATA_DIR / "email_suggestions.jsonl"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _append_event(event: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _append_lock, _events_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_events() -> list[dict]:
    path = _events_path()
    if not path.is_file():
        return []
    events: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not event.get("event") or not event.get("id"):
                raise ValueError("event and id are required")
            events.append(event)
        except (json.JSONDecodeError, ValueError) as exc:
            print(
                f"warning: skipped malformed email-suggestion line {line_number}: {exc}",
                file=sys.stderr,
            )
    return events


def read_suggestions() -> list[dict]:
    suggestions: dict[str, dict] = {}
    order: list[str] = []
    for event in _read_events():
        suggestion_id = str(event["id"])
        kind = event["event"]
        if kind == "suggested":
            if suggestion_id not in suggestions:
                order.append(suggestion_id)
                suggestions[suggestion_id] = {**event, "resolution": "pending"}
            elif suggestions[suggestion_id].get("resolution") == "pending":
                suggestions[suggestion_id] = {**event, "resolution": "pending"}
        elif kind in {"confirmed", "dismissed"} and suggestion_id in suggestions:
            suggestions[suggestion_id]["resolution"] = kind
            suggestions[suggestion_id]["resolved_at"] = event.get("at", "")
    return [suggestions[suggestion_id] for suggestion_id in order]


def pending_suggestions() -> list[dict]:
    return [
        suggestion
        for suggestion in read_suggestions()
        if suggestion.get("resolution") == "pending"
    ]


def _load_credentials() -> Credentials:
    if not config.GMAIL_TOKEN_PATH.is_file():
        raise ValueError(
            "Gmail OAuth token not found; run "
            "'python -m backend.app.email_tracker authorize' first"
        )
    try:
        credentials = Credentials.from_authorized_user_file(
            str(config.GMAIL_TOKEN_PATH),
            scopes=[GMAIL_READONLY_SCOPE],
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read Gmail OAuth token: {exc}") from exc
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        config.GMAIL_TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        raise ValueError(
            "Gmail OAuth token is invalid; run "
            "'python -m backend.app.email_tracker authorize' again"
        )
    return credentials


def _gmail_service():
    return build("gmail", "v1", credentials=_load_credentials(), cache_discovery=False)


def authorize() -> Path:
    if not config.GMAIL_CLIENT_SECRET_PATH.is_file():
        raise ValueError(
            f"Gmail OAuth client file not found at {config.GMAIL_CLIENT_SECRET_PATH}; "
            "follow README's Email tracking setup first"
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.GMAIL_CLIENT_SECRET_PATH),
        scopes=[GMAIL_READONLY_SCOPE],
    )
    credentials = flow.run_local_server(port=0)
    config.GMAIL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
    return config.GMAIL_TOKEN_PATH


def _decode_body_data(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode(
        "utf-8", errors="replace"
    )


def _find_body_part(payload: dict, mime_type: str) -> str | None:
    if str(payload.get("mimeType", "")).lower() == mime_type:
        body = payload.get("body", {})
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, str) and data:
            return _decode_body_data(data)
    parts = payload.get("parts", [])
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            found = _find_body_part(part, mime_type)
            if found is not None:
                return found
    return None


def _html_to_text(content: str) -> str:
    unescaped = html.unescape(content)
    without_tags = re.sub(r"<[^>]+>", " ", unescaped)
    return " ".join(without_tags.split())


def _extract_message(message: dict) -> dict:
    payload = message.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    headers = {}
    for header in payload.get("headers", []):
        if isinstance(header, dict) and header.get("name"):
            headers[str(header["name"]).lower()] = str(header.get("value", ""))
    body = _find_body_part(payload, "text/plain")
    if body is None:
        body = _html_to_text(_find_body_part(payload, "text/html") or "")
    return {
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "body": body[: config.MAX_JOB_TEXT_CHARS],
    }


def _classify_email(subject: str, body: str) -> tuple[str | None, str]:
    text = f"{subject}\n{body}"
    for status, pattern in _KEYWORD_RULES:
        match = pattern.search(text)
        if match:
            return status, f'Keyword match: "{match.group(0)}"'[:200]
    status = llm.classify_application_email(subject, body)
    if status not in _EMAIL_OUTCOMES:
        return None, ""
    evidence = f"Local model classified this message as {status}."
    return status, evidence[:200]


def _find_matching_application(
    sender: str,
    subject: str,
    body: str,
    applications: list[dict],
) -> dict | None:
    haystack = f"{sender} {subject} {body}".lower()
    candidates = []
    for application in applications:
        company = str(application.get("company") or "").strip().lower()
        if application.get("event") not in {"compiled", "applied"} or not company:
            continue
        if re.search(rf"(?<!\w){re.escape(company)}(?!\w)", haystack):
            candidates.append(application)
    if not candidates:
        return None
    return max(candidates, key=lambda application: str(application.get("at", "")))


def _list_message_ids(service) -> list[str]:
    message_ids: list[str] = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": GMAIL_QUERY}
        if page_token:
            kwargs["pageToken"] = page_token
        payload = service.users().messages().list(**kwargs).execute()
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        for message in messages:
            if isinstance(message, dict) and message.get("id"):
                message_ids.append(str(message["id"]))
        page_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
        if not page_token:
            return message_ids


def _record_seen(message_id: str) -> dict:
    event = {"event": "seen", "id": message_id, "at": _timestamp()}
    _append_event(event)
    return event


def _record_suggested(
    message_id: str,
    *,
    application: dict,
    status: str,
    evidence: str,
    sender: str,
    subject: str,
) -> dict:
    event = {
        "event": "suggested",
        "kind": "outcome",
        "id": message_id,
        "at": _timestamp(),
        "application_id": str(application["id"]),
        "company": str(application.get("company", "")),
        "role": str(application.get("role", "")),
        "status": status,
        "evidence": evidence[:200],
        "sender": sender,
        "subject": subject,
    }
    _append_event(event)
    return event


def _record_new_application_suggestion(
    message_id: str,
    *,
    company: str,
    role: str,
    evidence: str,
    sender: str,
    subject: str,
) -> dict:
    event = {
        "event": "suggested",
        "kind": "new_application",
        "id": message_id,
        "at": _timestamp(),
        "company": company,
        "role": role,
        "status": "applied",
        "evidence": evidence[:200],
        "sender": sender,
        "subject": subject,
    }
    _append_event(event)
    return event


def _record_unmatched(
    message_id: str,
    *,
    status: str,
    evidence: str,
    sender: str,
    subject: str,
) -> dict:
    event = {
        "event": "unmatched",
        "id": message_id,
        "at": _timestamp(),
        "status": status,
        "evidence": evidence[:200],
        "sender": sender,
        "subject": subject,
    }
    _append_event(event)
    return event


def poll() -> list[dict]:
    service = _gmail_service()
    seen_ids = {
        str(event["id"])
        for event in _read_events()
        if event.get("event") == "seen"
    }
    applications = tracker.read_applications()
    processed: list[dict] = []
    for message_id in _list_message_ids(service):
        if message_id in seen_ids:
            continue
        message = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        extracted = _extract_message(message)
        status, evidence = _classify_email(extracted["subject"], extracted["body"])
        if status is None:
            processed.append(_record_seen(message_id))
            seen_ids.add(message_id)
            continue
        application = _find_matching_application(
            extracted["sender"],
            extracted["subject"],
            extracted["body"],
            applications,
        )
        if application is None and status == "applied":
            company, role = llm.extract_application_details(
                extracted["subject"],
                extracted["body"],
                extracted["sender"],
            )
            event = _record_new_application_suggestion(
                message_id,
                company=company,
                role=role,
                evidence=evidence,
                sender=extracted["sender"],
                subject=extracted["subject"],
            )
        elif application is None:
            event = _record_unmatched(
                message_id,
                status=status,
                evidence=evidence,
                sender=extracted["sender"],
                subject=extracted["subject"],
            )
        else:
            event = _record_suggested(
                message_id,
                application=application,
                status=status,
                evidence=evidence,
                sender=extracted["sender"],
                subject=extracted["subject"],
            )
        _record_seen(message_id)
        seen_ids.add(message_id)
        processed.append(event)
    return processed


def _resolve_suggestion(identifier: str, suggestions: list[dict]) -> dict:
    if not suggestions:
        raise ValueError("no pending email suggestions")
    matches = [
        suggestion
        for suggestion in suggestions
        if str(suggestion["id"]).startswith(identifier)
    ]
    if not matches:
        raise ValueError(f"no email suggestion matches id prefix '{identifier}'")
    if len(matches) > 1:
        ids = ", ".join(str(suggestion["id"])[:12] for suggestion in matches)
        raise ValueError(f"ambiguous id prefix '{identifier}'; matches: {ids}")
    return matches[0]


def confirm_suggestion(identifier: str) -> str:
    suggestion = _resolve_suggestion(identifier, pending_suggestions())
    if suggestion.get("kind") == "new_application":
        tracker.record_applied(
            company=str(suggestion.get("company", "Company")),
            role=str(suggestion.get("role", "Role")),
            note="Detected from an email confirmation.",
        )
    else:
        tracker.record_outcome(
            str(suggestion["application_id"]),
            str(suggestion["status"]),
        )
    suggestion_id = str(suggestion["id"])
    _append_event({"event": "confirmed", "id": suggestion_id, "at": _timestamp()})
    return suggestion_id


def dismiss_suggestion(identifier: str) -> str:
    suggestion = _resolve_suggestion(identifier, pending_suggestions())
    suggestion_id = str(suggestion["id"])
    _append_event({"event": "dismissed", "id": suggestion_id, "at": _timestamp()})
    return suggestion_id


def _list_pending() -> int:
    suggestions = pending_suggestions()
    if not suggestions:
        print("No pending email suggestions.")
        return 0
    for suggestion in suggestions:
        label = (
            "NEW APPLICATION"
            if suggestion.get("kind") == "new_application"
            else suggestion.get("status", "")
        )
        print(
            f"{str(suggestion['id'])[:12]}  {suggestion.get('company', '')} — "
            f"{suggestion.get('role', '')}  {label}  "
            f"{str(suggestion.get('evidence', ''))[:200]}"
        )
    return 0


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    parser = argparse.ArgumentParser(
        description="Review-gated Gmail application-status suggestions"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("authorize", help="complete the one-time Gmail OAuth flow")
    subparsers.add_parser("poll", help="fetch and stage new status suggestions")
    subparsers.add_parser("pending", help="list unreviewed suggestions")
    confirm = subparsers.add_parser("confirm", help="apply one suggestion to the tracker")
    confirm.add_argument("identifier", help="unambiguous suggestion id prefix")
    dismiss = subparsers.add_parser("dismiss", help="dismiss one suggestion")
    dismiss.add_argument("identifier", help="unambiguous suggestion id prefix")
    args = parser.parse_args(argv)
    try:
        if args.command == "authorize":
            print(authorize())
            return 0
        if args.command == "poll":
            events = poll()
            suggestions = sum(event["event"] == "suggested" for event in events)
            unmatched = sum(event["event"] == "unmatched" for event in events)
            print(
                f"Processed {len(events)} new message(s): "
                f"{suggestions} suggestion(s), {unmatched} unmatched."
            )
            return 0
        if args.command == "pending":
            return _list_pending()
        if args.command == "confirm":
            print(confirm_suggestion(args.identifier))
            return 0
        print(dismiss_suggestion(args.identifier))
        return 0
    except (ValueError, llm.LLMError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
