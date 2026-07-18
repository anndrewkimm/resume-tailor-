import argparse
import html
import json
import sys
import threading
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from . import config


OUTCOME_STATUSES = ("applied", "screen", "interview", "offer", "rejected", "ghosted")
_append_lock = threading.Lock()


def _events_path() -> Path:
    return config.DATA_DIR / "applications.jsonl"


def _append_event(event: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _append_lock, _events_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def record_compiled(
    *,
    company: str,
    role: str,
    filename: str,
    edits_applied: int,
    fit_score: int | None,
    keywords_total: int | None,
    keywords_matched: int | None,
) -> str:
    application_id = uuid.uuid4().hex
    _append_event(
        {
            "event": "compiled",
            "id": application_id,
            "at": _timestamp(),
            "company": company,
            "role": role,
            "filename": filename,
            "edits_applied": edits_applied,
            "fit_score": fit_score,
            "keywords_total": keywords_total,
            "keywords_matched": keywords_matched,
        }
    )
    return application_id


def record_letter(*, company: str, role: str, filename: str) -> str:
    applications = read_applications()
    matching = [
        item
        for item in applications
        if item.get("company") == company and item.get("role") == role
    ]
    application_id = matching[-1]["id"] if matching else uuid.uuid4().hex
    _append_event(
        {
            "event": "letter",
            "id": application_id,
            "at": _timestamp(),
            "company": company,
            "role": role,
            "filename": filename,
        }
    )
    return application_id


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
            print(f"warning: skipped malformed tracker line {line_number}: {exc}", file=sys.stderr)
    return events


def read_applications() -> list[dict]:
    applications: dict[str, dict] = {}
    order: list[str] = []
    for event in _read_events():
        application_id = str(event["id"])
        kind = event["event"]
        if kind == "compiled":
            if application_id not in applications:
                order.append(application_id)
            applications[application_id] = {
                **event,
                "status": "compiled",
                "note": "",
            }
        elif kind == "letter":
            if application_id not in applications:
                order.append(application_id)
                applications[application_id] = {
                    "event": "letter",
                    "id": application_id,
                    "at": event.get("at", ""),
                    "company": event.get("company", "Company"),
                    "role": event.get("role", "Role"),
                    "filename": "",
                    "edits_applied": 0,
                    "fit_score": None,
                    "keywords_total": None,
                    "keywords_matched": None,
                    "status": "letter",
                    "note": "",
                }
            applications[application_id]["cover_letter_filename"] = event.get("filename", "")
        elif kind == "outcome" and application_id in applications:
            applications[application_id]["status"] = event.get("status", applications[application_id]["status"])
            applications[application_id]["note"] = event.get("note", "")
            applications[application_id]["outcome_at"] = event.get("at", "")
    return [applications[application_id] for application_id in order if application_id in applications]


def _resolve_application(identifier: str, applications: list[dict]) -> dict:
    compiled = [item for item in applications if item.get("event") == "compiled"]
    if not compiled:
        raise ValueError("no tracked applications")
    if identifier == "latest":
        return compiled[-1]
    matches = [item for item in compiled if str(item["id"]).startswith(identifier)]
    if not matches:
        raise ValueError(f"no application matches id prefix '{identifier}'")
    if len(matches) > 1:
        ids = ", ".join(str(item["id"])[:8] for item in matches)
        raise ValueError(f"ambiguous id prefix '{identifier}'; matches: {ids}")
    return matches[0]


def record_outcome(identifier: str, status: str, note: str = "") -> str:
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"invalid outcome '{status}'")
    application = _resolve_application(identifier, read_applications())
    _append_event(
        {
            "event": "outcome",
            "id": application["id"],
            "at": _timestamp(),
            "status": status,
            "note": note,
        }
    )
    return str(application["id"])


def generate_report(output_path: Path | None = None) -> Path:
    applications = read_applications()
    counts = Counter(item.get("status", "compiled") for item in applications)
    funnel = "".join(
        f"<li><strong>{html.escape(status.title())}</strong>: {count}</li>"
        for status, count in sorted(counts.items())
    ) or "<li>No applications tracked yet.</li>"
    rows = []
    for item in applications:
        at = str(item.get("at", ""))[:10]
        cells = (
            at,
            item.get("company", ""),
            item.get("role", ""),
            "—" if item.get("fit_score") is None else str(item["fit_score"]),
            str(item.get("edits_applied", 0)),
            item.get("status", "compiled"),
            item.get("note", ""),
        )
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>")
    body_rows = "".join(rows) or '<tr><td colspan="7">No applications tracked yet.</td></tr>'
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Application report</title><style>
body{{font:15px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#1c2a21}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border:1px solid #cbd6cc;text-align:left}}
th{{background:#e8f0e9}}ul{{display:flex;gap:20px;flex-wrap:wrap;padding:0;list-style:none}}
</style></head><body><h1>Application report</h1><ul>{funnel}</ul>
<table><thead><tr><th>Date</th><th>Company</th><th>Role</th><th>Fit</th><th>Edits</th><th>Outcome</th><th>Note</th></tr></thead>
<tbody>{body_rows}</tbody></table></body></html>"""
    destination = output_path or (config.OUTPUT_DIR / "applications_report.html")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination


def _list_applications() -> int:
    for item in read_applications():
        fit = "—" if item.get("fit_score") is None else f"{item['fit_score']}%"
        print(
            f"{str(item['id'])[:8]}  {str(item.get('at', ''))[:10]}  "
            f"{item.get('company', '')} — {item.get('role', '')}  fit {fit}  {item.get('status', 'compiled')}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local application tracker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list tracked applications")
    outcome = subparsers.add_parser("outcome", help="record an application outcome")
    outcome.add_argument("identifier", help="unique id prefix or 'latest'")
    outcome.add_argument("status", choices=OUTCOME_STATUSES)
    outcome.add_argument("--note", default="")
    subparsers.add_parser("report", help="write the local HTML report")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _list_applications()
        if args.command == "outcome":
            record_outcome(args.identifier, args.status, args.note)
            return 0
        destination = generate_report()
        print(destination)
        return 0
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
