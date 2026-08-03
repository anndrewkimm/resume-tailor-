# Task: add Lever and Ashby to job discovery alongside Greenhouse

## Why this task exists (read this first)

`CODEX_TASK_job_discovery.md` (§15 of PLAN.md) shipped v1 scoped to
Greenhouse only, and named Lever, Ashby, and Workable as explicit
fast-follows. This task adds Lever and Ashby (see §16.2 for why Workable
is left out — not researched yet, and two platforms is enough to prove the
multi-platform shape works before adding a third). Both are, like
Greenhouse, ATS vendors that expose a public, unauthenticated job-board
JSON API for any company using them — no scraping, no auth, consistent
with the "poll ATS APIs, not portal HTML" reasoning in §15 that keeps this
feature out of the "permanent maintenance treadmill" category §13.2
rejected.

**Both API shapes below were live-verified against real boards while
writing this spec** (same rigor as the original Greenhouse hardening
pass, which caught a real HTML-entity-encoding bug this way) — this is not
written from documentation or memory.

- Lever: `GET https://api.lever.co/v0/postings/{slug}?mode=json` — verified
  against `palantir` (302 live postings) and the empty `lever` board (200,
  `[]`). An unknown slug 404s (`{"ok":false,"error":"Document not found"}`).
  The response is a **bare JSON array**, not wrapped in a `{"jobs": [...]}`
  envelope like Greenhouse.
- Ashby: `GET https://api.ashbyhq.com/posting-api/job-board/{slug}` —
  verified against `ramp` (125 live postings). An unknown slug 404s with a
  plain-text `Not Found` body (not JSON — irrelevant here because
  `raise_for_status()` throws before `.json()` is ever called on a 404,
  same as it already does for a bad Greenhouse slug).

Real field-shape findings that change the implementation below:

- Both platforms already return **plain-text** descriptions
  (`descriptionPlain`) alongside HTML ones — unlike Greenhouse, no
  `html.unescape` + tag-stripping is needed for Lever/Ashby text.
- Ashby's real `title` field had a leading space in production data
  (`" Security Engineer, Cloud"`, verified on the live `ramp` board) —
  titles must be `.strip()`-ped before storing/matching.
- One real Lever posting (`palantir`) contains a mojibake character in
  `descriptionPlain` (`world's` renders as `world�s`) — the source
  data itself has a bad byte, not a bug in `httpx`'s decoding. This is a
  cosmetic, one-off, upstream data-quality issue like the existing
  Windows-console em-dash mojibake noted in §15's landing note — **do not
  attempt to fix or work around it**; it doesn't affect matching, scoring,
  or safety.

## Scope

Backend only: `backend/app/discovery.py`, `backend/app/models.py`,
`backend/tests/test_discovery.py`, and a short `README.md` update. No
changes to `main.py`, `security.py`, the extension, or `fit.py`.
**Deliberately keep the existing posting-ID format** (`f"{company.slug}:
{external_id}"`, `discovery.py:192`) unchanged rather than namespacing it
by platform — a hand-curated list of a few dozen companies picked by the
user has effectively no chance of two *different* companies sharing a
slug across platforms, and changing the format would break the id
assertions in roughly ten existing tests in `test_discovery.py` for no
real benefit. Don't do it.

## Backend changes

### 1. `backend/app/models.py`

Widen `Company.platform` (`models.py:8`):

```python
platform: Literal["greenhouse", "lever", "ashby"]
```

### 2. `backend/app/discovery.py`

Add two fetchers next to `fetch_greenhouse_jobs` (`discovery.py:49-60`),
matching its validation style exactly:

```python
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
```

Factor the whitespace-collapse tail of `_strip_html` (`discovery.py:63-66`)
into a shared helper, then reuse it for the two new platforms' already-
plain text:

```python
def _clean_text(text: str) -> str:
    return " ".join(text.split())[: config.MAX_JOB_TEXT_CHARS]


def _strip_html(content: str) -> str:
    unescaped = html.unescape(content)
    without_tags = re.sub(r"<[^>]+>", " ", unescaped)
    return _clean_text(without_tags)
```

Add per-platform normalizers that map each vendor's job dict to one common
shape (`external_id`, `title`, `url`, `location`, `description`):

```python
def _normalize_greenhouse_job(job: dict) -> dict:
    location_value = job.get("location", {})
    location = str(location_value.get("name", "")) if isinstance(location_value, dict) else ""
    return {
        "external_id": job["id"],
        "title": str(job["title"]).strip(),
        "url": str(job["absolute_url"]),
        "location": location,
        "description": _strip_html(str(job.get("content") or "")),
    }


def _normalize_lever_job(job: dict) -> dict:
    categories = job.get("categories", {})
    location = str(categories.get("location", "")) if isinstance(categories, dict) else ""
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
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
}
_NORMALIZERS = {
    "greenhouse": _normalize_greenhouse_job,
    "lever": _normalize_lever_job,
    "ashby": _normalize_ashby_job,
}
```

Rewrite `poll()`'s per-company/per-job body (`discovery.py:170-208`) to go
through the dispatch tables instead of calling `fetch_greenhouse_jobs`
directly and inlining Greenhouse-specific field access:

```python
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
```

This is a like-for-like behavior preservation for Greenhouse (same id
format, same title/location/description extraction, same per-company and
per-job exception handling) — the only change is routing through
`company.platform` instead of assuming Greenhouse.

### 3. `README.md`

In "Finding postings" (`README.md:123-151`), update the `platform` field
description to say it accepts `"greenhouse"`, `"lever"`, or `"ashby"`, and
add one line each on where to find the slug for the new platforms: Lever
boards are `https://jobs.lever.co/{slug}`; Ashby boards are
`https://jobs.ashbyhq.com/{slug}`.

## Verification

- Update `test_load_companies_config_rejects_unknown_platform`
  (`test_discovery.py:94-97`) — `"lever"` is no longer an invalid
  platform, so this test would now fail for the wrong reason. Change the
  fixture to a still-genuinely-unsupported value (e.g. `"workable"`) so
  the test keeps covering "reject an unknown platform," just not with the
  value this task just legalized.
- New unit tests mirroring `test_fetch_greenhouse_jobs_uses_expected_endpoint_and_shape`
  (`test_discovery.py:107-121`) for `fetch_lever_jobs` and
  `fetch_ashby_jobs`: correct URL construction, and that a non-list (Lever)
  / missing-`jobs`-key (Ashby) response raises `ValueError`.
- New unit tests for `_normalize_lever_job` and `_normalize_ashby_job`
  using realistic fixtures modeled on the verified shapes above (include
  a title with a stray leading space in the Ashby fixture, matching real
  production data, and assert it comes out stripped).
- Extend `test_poll_filters_titles_and_does_not_duplicate_seen_events`-style
  coverage (`test_discovery.py:156-172`) with a companies list mixing all
  three platforms in one `poll()` call, asserting each posting's `platform`
  field and `description` came from the right normalizer.
- `python -m unittest discover -s backend/tests -v` passes (all existing
  Greenhouse-only tests must keep passing unmodified — this is the check
  that the id-format and per-job-exception-handling preservation actually
  held).
- Manual: temporarily add one real Lever company (e.g. `{"name": "Palantir",
  "platform": "lever", "slug": "palantir"}`) and one real Ashby company
  (e.g. `{"name": "Ramp", "platform": "ashby", "slug": "ramp"}`) to a local
  `companies.json`, run `python -m backend.app.discovery poll`, confirm
  postings from both new platforms are recorded with sane titles/locations/
  URLs, then remove the temporary entries (or leave them if the user wants
  to keep them — that's a curation choice, not part of this task).
