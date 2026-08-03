"""Exercise a real authorized Gmail inbox and the review-gated poller."""

import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import email_tracker


def main() -> None:
    service = email_tracker._gmail_service()
    message_ids = email_tracker._list_message_ids(service)
    if not message_ids:
        raise RuntimeError("Gmail smoke requires at least one message from the last 180 days")
    message = (
        service.users()
        .messages()
        .get(userId="me", id=message_ids[0], format="full")
        .execute()
    )
    extracted = email_tracker._extract_message(message)
    if not isinstance(extracted["body"], str):
        raise RuntimeError("Gmail message body extraction did not return text")

    processed = email_tracker.poll()
    print(
        "Gmail smoke passed: credentials loaded without a consent prompt, "
        f"one real message body was extracted, and poll processed {len(processed)} new message(s)."
    )


if __name__ == "__main__":
    main()
