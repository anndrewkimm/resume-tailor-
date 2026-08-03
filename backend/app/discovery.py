import argparse
import html
import json
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config
from .fit import compute_fit
from .llm import extract_keywords
from .models import CompaniesConfig


DISCOVERY_STATUSES = ("dismissed", "tailored")
_STATUS_COMMANDS = {"dismiss": "dismissed", "tailored": "tailored"}
_POLL_DELAY_SECONDS = 0.3
_append_lock = threading.Lock()


def _events_path() -> Path:
    return config.DATA_DIR / "discovered_jobs.jsonl"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _append_event(event: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _append_lock, _events_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_companies_config() -> CompaniesConfig:
    path = config.COMPANIES_PATH
    if not path.is_file():
        print("no companies.json found — see README", file=sys.stderr)
        return CompaniesConfig()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CompaniesConfig.model_validate(payload)


def fetch_greenhouse_jobs(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url)
        response.raise_for_status()
    payload = response.json()
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("Greenhouse response did not contain a jobs array")
    if not all(isinstance(job, dict) for job in jobs):
        raise ValueError("Greenhouse jobs array contained a non-object item")
    return jobs


def fetch_lever_jobs(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url)
        response.raise_for_status()
    jobs = response.json()
    if not isinstance(jobs, list):
        raise ValueError("Lever response was not a list of postings")
    if not all(isinstance(job, dict) for job in jobs):
        raise ValueError("Lever postings array contained a non-object item")
    return jobs


def fetch_ashby_jobs(slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url)
        response.raise_for_status()
    payload = response.json()
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("Ashby response did not contain a jobs array")
    if not all(isinstance(job, dict) for job in jobs):
        raise ValueError("Ashby jobs array contained a non-object item")
    return jobs


def _clean_text(text: str) -> str:
    return " ".join(text.split())[: config.MAX_JOB_TEXT_CHARS]


def _strip_html(content: str) -> str:
    unescaped = html.unescape(content)
    without_tags = re.sub(r"<[^>]+>", " ", unescaped)
    return _clean_text(without_tags)


def _normalize_greenhouse_job(job: dict) -> dict:
    location_value = job.get("location", {})
    location = (
        str(location_value.get("name", "")) if isinstance(location_value, dict) else ""
    )
    return {
        "external_id": job["id"],
        "title": str(job["title"]).strip(),
        "url": str(job["absolute_url"]),
        "location": location,
        "description": _strip_html(str(job.get("content") or "")),
    }


def _normalize_lever_job(job: dict) -> dict:
    categories = job.get("categories", {})
    location = (
        str(categories.get("location", "")) if isinstance(categories, dict) else ""
    )
    return {
        "external_id": job["id"],
        "title": str(job["text"]).strip(),
        "url": str(job["hostedUrl"]),
        "location": location,
        "description": _clean_text(str(job.get("descriptionPlain") or "")),
    }


def _normalize_ashby_job(job: dict) -> dict:
    return {
        "external_id": job["id"],
        "title": str(job["title"]).strip(),
        "url": str(job["jobUrl"]),
        "location": str(job.get("location", "")),
        "description": _clean_text(str(job.get("descriptionPlain") or "")),
    }


_FETCHERS = {
    # Resolve through module globals at call time so tests and local tooling
    # can replace a fetcher without having to mutate this dispatch table too.
    "greenhouse": lambda slug: fetch_greenhouse_jobs(slug),
    "lever": lambda slug: fetch_lever_jobs(slug),
    "ashby": lambda slug: fetch_ashby_jobs(slug),
}
_NORMALIZERS = {
    "greenhouse": _normalize_greenhouse_job,
    "lever": _normalize_lever_job,
    "ashby": _normalize_ashby_job,
}


def record_seen(
    *,
    id: str,
    company: str,
    role: str,
    location: str,
    url: str,
    platform: str,
    description: str,
) -> None:
    _append_event(
        {
            "event": "seen",
            "id": id,
            "at": _timestamp(),
            "company": company,
            "role": role,
            "location": location,
            "url": url,
            "platform": platform,
            "description": description,
        }
    )


def record_status(id: str, status: str) -> None:
    if status not in DISCOVERY_STATUSES:
        raise ValueError(f"invalid discovery status '{status}'")
    _append_event({"event": "status", "id": id, "at": _timestamp(), "status": status})


def record_fit(
    id: str,
    *,
    fit_score: int,
    keywords_total: int,
    keywords_matched: int,
) -> None:
    if not 0 <= fit_score <= 100:
        raise ValueError("fit_score must be between 0 and 100")
    if keywords_total < 0 or not 0 <= keywords_matched <= keywords_total:
        raise ValueError("keyword counts are inconsistent")
    _append_event(
        {
            "event": "fit",
            "id": id,
            "at": _timestamp(),
            "fit_score": fit_score,
            "keywords_total": keywords_total,
            "keywords_matched": keywords_matched,
        }
    )


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
            print(f"warning: skipped malformed discovery line {line_number}: {exc}", file=sys.stderr)
    return events


def read_postings() -> list[dict]:
    postings: dict[str, dict] = {}
    order: list[str] = []
    for event in _read_events():
        posting_id = str(event["id"])
        kind = event["event"]
        if kind == "seen":
            if posting_id not in postings:
                order.append(posting_id)
                postings[posting_id] = dict(event)
        elif kind == "status" and posting_id in postings:
            postings[posting_id]["status"] = event.get("status", "")
            postings[posting_id]["status_at"] = event.get("at", "")
        elif kind == "fit" and posting_id in postings:
            for field in ("fit_score", "keywords_total", "keywords_matched"):
                postings[posting_id][field] = event.get(field)
            postings[posting_id]["fit_at"] = event.get("at", "")
    return [postings[posting_id] for posting_id in order]


def _title_matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    return any(re.search(rf"\b{re.escape(keyword)}\b", title, re.IGNORECASE) for keyword in keywords)


def poll() -> list[dict]:
    companies_config = load_companies_config()
    known_ids = {str(posting["id"]) for posting in read_postings()}
    new_ids: list[str] = []

    for index, company in enumerate(companies_config.companies):
        try:
            jobs = _FETCHERS[company.platform](company.slug)
        except Exception as exc:
            print(f"warning: could not poll {company.name} ({company.slug}): {exc}", file=sys.stderr)
            jobs = []

        normalize = _NORMALIZERS[company.platform]
        for job in jobs:
            try:
                normalized = normalize(job)
            except (KeyError, TypeError, ValueError) as exc:
                print(f"warning: skipped malformed job from {company.name}: {exc}", file=sys.stderr)
                continue

            title = normalized["title"]
            if not _title_matches(title, companies_config.title_keywords):
                continue
            posting_id = f"{company.slug}:{normalized['external_id']}"
            if posting_id in known_ids:
                continue
            record_seen(
                id=posting_id,
                company=company.name,
                role=title,
                location=normalized["location"],
                url=normalized["url"],
                platform=company.platform,
                description=normalized["description"],
            )
            known_ids.add(posting_id)
            new_ids.append(posting_id)

        if index < len(companies_config.companies) - 1:
            time.sleep(_POLL_DELAY_SECONDS)

    postings_by_id = {str(posting["id"]): posting for posting in read_postings()}
    return [postings_by_id[posting_id] for posting_id in new_ids]


def score_new() -> list[dict]:
    source = config.RESUME_TEX_PATH.read_text(encoding="utf-8")
    scored: list[dict] = []
    for posting in read_postings():
        if "fit_score" in posting:
            continue
        try:
            analysis = extract_keywords(str(posting.get("description", "")))
            fit = compute_fit(source, analysis.keywords)
        except Exception as exc:
            print(f"warning: could not score {posting['id']}: {exc}", file=sys.stderr)
            continue
        record_fit(
            str(posting["id"]),
            fit_score=fit.score,
            keywords_total=len(analysis.keywords),
            keywords_matched=len(fit.matched),
        )
        scored.append(
            {
                **posting,
                "fit_score": fit.score,
                "keywords_total": len(analysis.keywords),
                "keywords_matched": len(fit.matched),
            }
        )
    return scored


def _safe_report_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "#"
    return value


def generate_report(output_path: Path | None = None) -> Path:
    postings = read_postings()
    postings.sort(key=lambda item: str(item.get("at", "")), reverse=True)
    postings.sort(
        key=lambda item: (
            item.get("fit_score") is not None,
            int(item.get("fit_score") or 0),
        ),
        reverse=True,
    )

    rows = []
    for item in postings:
        date = html.escape(str(item.get("at", ""))[:10])
        company = html.escape(str(item.get("company", "")))
        role = html.escape(str(item.get("role", "")))
        location = html.escape(str(item.get("location", "")))
        url = html.escape(_safe_report_url(str(item.get("url", ""))), quote=True)
        fit = "—" if item.get("fit_score") is None else f"{int(item['fit_score'])}%"
        status = html.escape(str(item.get("status") or "new"))
        rows.append(
            f'<tr><td>{date}</td><td>{company}</td><td><a href="{url}">{role}</a></td>'
            f"<td>{location}</td><td>{fit}</td><td>{status}</td></tr>"
        )
    body_rows = "".join(rows) or '<tr><td colspan="6">No postings discovered yet.</td></tr>'
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Discovered jobs report</title><style>
body{{font:15px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#1c2a21}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border:1px solid #cbd6cc;text-align:left}}
th{{background:#e8f0e9}}a{{color:#175c35}}
</style></head><body><h1>Discovered jobs</h1>
<table><thead><tr><th>First seen</th><th>Company</th><th>Role</th><th>Location</th><th>Fit</th><th>Status</th></tr></thead>
<tbody>{body_rows}</tbody></table></body></html>"""
    destination = output_path or (config.OUTPUT_DIR / "discovered_jobs_report.html")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination


def _resolve_posting(identifier: str, postings: list[dict]) -> dict:
    if not postings:
        raise ValueError("no discovered postings")
    matches = [posting for posting in postings if str(posting["id"]).startswith(identifier)]
    if not matches:
        raise ValueError(f"no posting matches id prefix '{identifier}'")
    if len(matches) > 1:
        ids = ", ".join(str(posting["id"]) for posting in matches)
        raise ValueError(f"ambiguous id prefix '{identifier}'; matches: {ids}")
    return matches[0]


def _set_status(identifier: str, status: str) -> str:
    posting = _resolve_posting(identifier, read_postings())
    posting_id = str(posting["id"])
    record_status(posting_id, status)
    return posting_id


def _list_postings(*, new_only: bool = False, min_fit: int | None = None) -> int:
    if min_fit is not None and not 0 <= min_fit <= 100:
        raise ValueError("--min-fit must be between 0 and 100")
    postings = sorted(read_postings(), key=lambda item: str(item.get("at", "")), reverse=True)
    for item in postings:
        if new_only and item.get("status"):
            continue
        if min_fit is not None and (
            item.get("fit_score") is None or int(item["fit_score"]) < min_fit
        ):
            continue
        fit = "—" if item.get("fit_score") is None else f"{int(item['fit_score'])}%"
        status = str(item.get("status") or "new")
        print(
            f"{str(item['id'])[:12]}  {str(item.get('at', ''))[:10]}  "
            f"{item.get('company', '')} — {item.get('role', '')}  "
            f"fit {fit}  {status}  {item.get('url', '')}"
        )
    return 0


def _configure_console_encoding() -> None:
    """Keep third-party job titles printable on Windows consoles."""
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
    parser = argparse.ArgumentParser(description="Local small/mid-size job discovery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("poll", help="poll configured ATS boards")
    subparsers.add_parser("score", help="score postings that do not have a fit score")
    list_parser = subparsers.add_parser("list", help="list discovered postings")
    list_parser.add_argument("--new-only", action="store_true", help="hide dismissed/tailored postings")
    list_parser.add_argument("--min-fit", type=int, help="show only scored postings at or above N")
    for command, status in _STATUS_COMMANDS.items():
        status_parser = subparsers.add_parser(command, help=f"mark a posting {status}")
        status_parser.add_argument("identifier", help="unique posting id prefix")
    subparsers.add_parser("report", help="write the local HTML report")
    args = parser.parse_args(argv)

    try:
        if args.command == "poll":
            for posting in poll():
                print(
                    f"{posting.get('company', '')} — {posting.get('role', '')} — "
                    f"{posting.get('location', '')} — {posting.get('url', '')}"
                )
            return 0
        if args.command == "score":
            for posting in score_new():
                print(
                    f"{posting.get('company', '')} — {posting.get('role', '')} — "
                    f"fit {posting['fit_score']}%"
                )
            return 0
        if args.command == "list":
            return _list_postings(new_only=args.new_only, min_fit=args.min_fit)
        if args.command in _STATUS_COMMANDS:
            print(_set_status(args.identifier, _STATUS_COMMANDS[args.command]))
            return 0
        destination = generate_report()
        print(destination)
        return 0
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
