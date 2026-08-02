# Task: job discovery v1 — poll ATS job-board APIs for a curated small/mid-size company list, store locally, CLI to review

## Why this task exists (read this first)

This project has so far assumed the user finds a posting themselves and
opens it in a browser tab before invoking the extension. PLAN.md §13.2
deliberately rejected general portal scraping twice — as a volume play
("discover more postings") it didn't serve the project's stated goal
(quality of one application over volume of discovered postings) and it's a
permanent maintenance war against arbitrary portal DOM changes.

PLAN.md §15 (2026-08-01) reopened this with a different, narrower
rationale: the popular public "tech internship" tracking repos only bother
to curate big-name/target-school-pipeline companies, which structurally
disadvantages someone without that pedigree. The ask isn't "find more
postings," it's "find postings at smaller/mid-size companies that the
popular lists don't bother covering" — a targeting problem, not a volume
problem.

**The technical unlock that makes this tractable without reintroducing the
DOM-scraping maintenance problem**: most of those repos (and most ATS
platforms) already expose stable, public, versioned JSON APIs meant for
embedding a company's job board on its own site — e.g. Greenhouse's
`https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`.
Polling that is nothing like scraping arbitrary HTML: it's a documented API
contract that doesn't change on a redesign. It's also a fundamentally
different risk category from scraping LinkedIn/Indeed (aggressive anti-bot,
hostile ToS) — this is data the company itself publishes for exactly this
kind of consumption. **v1 targets Greenhouse only.** Other ATS platforms
(Lever, Ashby, Workable) follow the same shape and are a natural fast
follow, not part of this task.

**What this task deliberately does NOT do**: wire discovered postings into
`/tailor/start` directly, or build any extension UI. Every Greenhouse
posting has a real, public `absolute_url` — the bridge to the existing
pipeline is "open that URL in a browser tab, then use the extension exactly
as today." That keeps this task's integration surface at zero: no changes
to `main.py`, `security.py`, or the extension. This mirrors
`CODEX_TASK_application_tracker.md`'s own v1 scope decision (CLI first, UI
later) — same reasoning applies here even more strongly, since a
`job_text` ingestion path that bypasses the extension would duplicate the
edit-review UI outside the extension, which is a much bigger decision than
this task should make unilaterally.

## Scope

- New: `backend/app/discovery.py` (module + CLI), `companies.json` (repo
  root, committed — NOT gitignored, this is a curated list, not personal
  application history), `data/discovered_jobs.jsonl` (gitignored, already
  covered by the existing `data/` rule in `.gitignore`).
- Edited: `backend/app/config.py` (one new path constant), `README.md`
  (short new section, mirroring the existing "Tracking applications"
  section's style), `requirements.txt` only if a new dependency turns out
  to be necessary (it shouldn't — `httpx` is already a dependency and
  covers the HTTP calls; do not add PyYAML or an HTML-parsing library for
  this).
- Not touched: `main.py`, `security.py`, `jobs.py`, the extension. No new
  HTTP endpoints — same "CLI only, no unauthenticated localhost surface"
  reasoning as the tracker task applies here too, and there's an added
  reason: this task makes outbound calls to third-party APIs, which has no
  business happening from extension-reachable endpoints.

## Storage formats

### `companies.json` (repo root, hand-curated, committed)

A flat JSON array, meant to be hand-edited over time (the user's own
words: "seeded, then I curate"). Seeding it initially (pasting in company
names/slugs found elsewhere) is the user's job, not this task's — do not
invent or hardcode any real company slugs into the codebase.

```json
[
  {"name": "Example Co", "platform": "greenhouse", "slug": "examplecoinc"}
]
```

- `platform`: `Literal["greenhouse"]` for v1 — reject anything else at load
  time with a clear error naming the offending entry, don't silently skip
  it (this file is hand-edited; a typo should be loud).
- `slug`: the Greenhouse board token (the `{board_token}` in the API URL
  above — for a company at `https://boards.greenhouse.io/examplecoinc`,
  the slug is `examplecoinc`).
- Validate with a pydantic model (`Company`) next to the existing models in
  `models.py`, matching the rest of the codebase's convention of pydantic
  for all structured data — don't hand-roll dict validation here.
- Missing file → empty list with a one-line stderr note ("no companies.json
  found — see README"), not a crash; this file won't exist until the user
  creates it.

### `data/discovered_jobs.jsonl` (gitignored, machine-written)

Same event-sourced JSONL pattern as `data/applications.jsonl`
(`tracker.py:22-25,81-94`) — append-only, folded on read, malformed lines
skipped with a stderr warning rather than crashing. Two event shapes:

```json
{"event": "seen", "id": "<slug>:<external_id>", "at": "<UTC ISO-8601>",
 "company": "...", "role": "...", "location": "...", "url": "...",
 "platform": "greenhouse", "description": "<plain-text, HTML-stripped>"}

{"event": "status", "id": "<same id>", "at": "<UTC ISO-8601>",
 "status": "dismissed|tailored"}

{"event": "fit", "id": "<same id>", "at": "<UTC ISO-8601>",
 "fit_score": 78, "keywords_total": 12, "keywords_matched": 9}
```

Current state of a posting = its `seen` event folded with any later
`status`/`fit` events (last `status` wins; most recent `fit` wins) — same
folding shape as `tracker.read_applications()`. `id` is stable across polls
(`f"{company_slug}:{external_id}"`, where `external_id` is Greenhouse's own
numeric job id) so re-polling never duplicates a posting; a `seen` event is
only appended the first time an id is observed.

**Non-goal for v1**: detecting when a posting is *removed* from a
company's board (Greenhouse's list endpoint just stops returning it — there
is no "closed" event fired). Don't attempt to infer or record closure;
leave stale entries as-is. Note this as a known gap in the README section
rather than solving it now.

## Backend changes

### 1. `backend/app/config.py`

Add `COMPANIES_PATH = Path(os.environ.get("COMPANIES_PATH", REPO_ROOT / "companies.json"))`, following the exact pattern already used for `RESUME_TEX_PATH`/`RESUME_CLS_PATH` (`config.py:11-12`).

### 2. `models.py`

Add:

```python
class Company(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    platform: Literal["greenhouse"]
    slug: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9-]+$")
```

### 3. New file: `backend/app/discovery.py`

Module half:

- `load_companies() -> list[Company]` — reads `config.COMPANIES_PATH`,
  parses JSON, validates each entry as `Company`; missing file returns
  `[]` with the stderr note above; a malformed entry raises with the
  offending index/name in the message (loud, per the "hand-edited, typos
  should surface" reasoning above).
- `fetch_greenhouse_jobs(slug: str) -> list[dict]` — `httpx.Client` GET to
  `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`,
  10s timeout, matching the timeout discipline already used for the Ollama
  client in `llm.py`. Returns the parsed `jobs` array. **Before finalizing
  the field-name assumptions below, hit the live endpoint for 2-3 real
  Greenhouse company slugs and confirm the response shape** — this task's
  author is working from Greenhouse's documented/publicly-known board-API
  shape, not a verified-today response, and Greenhouse controls this
  contract. Expected shape per job: `id` (external id), `title`,
  `location.name`, `absolute_url`, `content` (HTML description).
- `_strip_html(content: str) -> str` — minimal regex-based tag strip
  (`re.sub(r"<[^>]+>", " ", content)`) plus `html.unescape`, then collapse
  whitespace; cap at `config.MAX_JOB_TEXT_CHARS` (reuse the existing
  constant from `config.py:26`, same cap `_check_job_size` enforces in
  `main.py`). Don't add an HTML-parsing dependency for this — a regex
  strip is adequate for feeding the LLM keyword extractor, it doesn't need
  to be a faithful render.
- `record_seen(*, id, company, role, location, url, platform, description) -> None` and `record_status(id, status) -> None` and `record_fit(id, *, fit_score, keywords_total, keywords_matched) -> None` — append-only writers, same `threading.Lock` + `config.DATA_DIR` pattern as `tracker.py:15,22-25` (new file: `data/discovered_jobs.jsonl`, separate from `applications.jsonl` — these are prospective postings, not the user's application history, and conflating the two files would break the tracker's existing read path).
- `read_postings() -> list[dict]` — parse + fold, mirroring
  `tracker._read_events`/`read_applications` (`tracker.py:81-133`).
- `poll() -> list[dict]` — for each company from `load_companies()`, call
  `fetch_greenhouse_jobs`; wrap each company's fetch in
  `try/except Exception` that prints a stderr warning and continues (one
  renamed/typo'd slug must not abort the whole poll — same defensiveness
  as the tracker's malformed-line handling). For each returned job whose
  id isn't already in `read_postings()`, call `record_seen(...)` and
  collect it. Return the list of newly seen postings (this is what makes
  a poll run "show me only what's new" instead of a full re-browse).
- `score_new() -> list[dict]` — for postings with a `seen` event but no
  `fit` event yet, call `extract_keywords` (`llm.py`) on the description,
  then `compute_fit` (`fit.py`) against the same base-resume read
  `main._base_resume()` already does (`config.RESUME_TEX_PATH.read_text(...)`
  — duplicate that one-liner here rather than importing from `main`, to
  keep `discovery.py` independent of the FastAPI app module), then
  `record_fit(...)`. **Deliberately a separate step from `poll`**, not
  folded into it: polling should stay fast and always-succeed (no LLM
  calls); scoring is slower (local Ollama call per posting) and is where a
  model hiccup should be retryable without re-polling.

CLI half (`python -m backend.app.discovery <subcommand>`, argparse,
`if __name__ == "__main__":` — same shape as `tracker.py:213-238`):

- `poll` — runs `poll()`, prints one line per newly discovered posting
  (company, role, location, url).
- `score` — runs `score_new()`, prints one line per posting scored
  (company, role, fit score).
- `list [--new-only] [--min-fit N]` — prints folded postings, most recent
  first: id prefix, date first seen, company, role, fit score (`—` if not
  yet scored), status, url. `--new-only` filters to status-less (not yet
  dismissed/tailored) postings; `--min-fit` filters by fit score when
  present.
- `dismiss <id-prefix>` / `tailored <id-prefix>` — append a `status` event.
  Reuse the same id-prefix-resolution shape as `tracker._resolve_application`
  (`tracker.py:136-148`) — unique prefix required, ambiguous prefix errors
  listing the matches. Write this as a local helper in `discovery.py`;
  don't import it from `tracker.py` — the two modules should stay
  independent (different entity, no reason to couple them).

### 4. `.gitignore`

No change needed — `data/` already covers `data/discovered_jobs.jsonl`.
Confirm `companies.json` at repo root is not caught by any existing rule
(it isn't — the `data/` rule is directory-scoped).

### 5. `README.md`

Short "Finding postings" section: what `companies.json` is and its format,
the four CLI commands, and the "open the posting URL, then use the
extension as normal" bridge to the existing tailor flow. Note the
non-goals explicitly: no automatic scheduling (the user runs `poll`
manually or wires it to their OS's own task scheduler — this project
doesn't manage a background daemon, consistent with everything else here
being request- or CLI-driven), and no closed-posting detection.

## Verification

- Unit tests (unittest style, in `backend/tests/`, new file
  `test_discovery.py`):
  - `load_companies` parses a valid file, rejects an unknown `platform`
    with a clear error, returns `[]` for a missing file.
  - Mock `httpx.Client` the same way `test_llm.py:20` does
    (`@patch("backend.app.discovery.httpx.Client")`) — `fetch_greenhouse_jobs`
    parses a representative fixture response into the expected job dicts;
    a non-200 response or connection error is caught in `poll()` (assert
    it's logged to stderr and other companies still get polled — use two
    companies in the fixture, one that fails).
  - `poll()` run twice against the same mocked response only appends
    `seen` events for new ids the second time (no duplicates).
  - `record_status`/`record_fit` then `read_postings()` round-trips and
    folds correctly; two `status` events → last wins.
  - A malformed line in `discovered_jobs.jsonl` is skipped, remaining
    lines still parse.
  - `dismiss`/`tailored` CLI id-prefix resolution: unique prefix works,
    ambiguous prefix errors.
  - `_strip_html` strips tags and unescapes entities from a fixture
    string.
  - Same test isolation pattern as `test_tracker.py:11-19`: monkeypatch
    `config.DATA_DIR` (and `config.COMPANIES_PATH`) to a
    `tempfile.TemporaryDirectory` for every test — never let a test run
    write the real `companies.json` or `data/discovered_jobs.jsonl`.
- `python -m unittest discover -s backend/tests -v` passes; a test run
  leaves the real repo's `data/` and `companies.json` untouched.
- Manual smoke check (not part of the automated suite, note in the PR/task
  notes instead): run `poll` against 2-3 real Greenhouse company slugs the
  user provides, confirm the response shape matches what
  `fetch_greenhouse_jobs` expects, and adjust field names if Greenhouse's
  live API differs from the assumed shape above.
