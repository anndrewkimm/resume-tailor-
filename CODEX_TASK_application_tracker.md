# Task: application tracker — log every compiled application, record outcomes, generate a local report

## Why this task exists (read this first)

Today the pipeline's memory is a folder of PDFs: `/compile` writes
`Resume_<Company>_<Role>.pdf` into `output/` (`main.py:181-183`) and nothing
else is retained — not when it was compiled, against which posting, with
which edits, what the fit score was, or what happened afterward. Once the
user is applying at any volume, "which of these did I actually send, and did
any get a response?" becomes unanswerable.

This task adds a small, local, append-only application log written by the
backend, a tiny CLI for recording outcomes later, and a self-contained HTML
report. Modeled on the tracker/`/outcome`/report ideas from a reviewed
public job-search repo (see PLAN.md §13), adapted to this project's shape.

**Deliberate design decision — no new HTTP endpoints.** Recording and
reporting happen via a CLI (`python -m backend.app.tracker ...`), not the
API, for a specific security reason: every existing endpoint is guarded by
`require_extension_origin` (`security.py`), which requires the
`X-Extension-Secret` header. A report page opened in a normal browser tab
can't send that header, so serving a report over HTTP would mean adding an
**unauthenticated** localhost GET endpoint — exactly the "any webpage's JS
can call a 127.0.0.1 port" surface PLAN.md §3.2 warns about. A static HTML
file opened via `file://` has no such surface. Do not add unauthenticated
endpoints to make this more convenient.

## Scope

- New: `backend/app/tracker.py` (log module + CLI), `data/` directory
  (gitignored).
- Edited: `backend/app/main.py` (one call in `/compile`'s success path),
  `.gitignore`, `README.md` (short new section), and — only if
  `CODEX_TASK_docker_single_command_setup.md` has already been implemented —
  add `./data:/app/data` to the compose file's volumes.
- Not touched: extension (no UI in v1), `security.py`, `resume_parser.py`.

## Storage format

`data/applications.jsonl` — append-only JSON Lines, one event per line.
JSONL over CSV because outcome updates arrive later (append an event rather
than rewrite a row) and fields like the edit list nest poorly in CSV; JSONL
over SQLite because a human-readable, hand-fixable text file suits a
single-user local tool and keeps the diff/backup story trivial.

Two event shapes, discriminated by `"event"`:

```json
{"event": "compiled", "id": "<uuid4hex>", "at": "<UTC ISO-8601>",
 "company": "...", "role": "...", "filename": "Resume_..._....pdf",
 "edits_applied": 4, "fit_score": 78, "keywords_total": 12, "keywords_matched": 9}

{"event": "outcome", "id": "<same id>", "at": "<UTC ISO-8601>",
 "status": "applied|screen|interview|offer|rejected|ghosted", "note": "..."}
```

Current state of an application = its `compiled` event folded with its
outcome events in file order (last outcome wins). Malformed lines must be
skipped with a warning to stderr, never crash the reader — the user may
hand-edit this file.

## Backend changes

### 1. New file: `backend/app/tracker.py`

Module half:

- `DATA_DIR` from env `DATA_DIR`, default `config.REPO_ROOT / "data"`
  (follow the existing pattern in `config.py:11-13` — actually put the
  constant in `config.py` next to `OUTPUT_DIR` and import it, keeping all
  path config in one place).
- `record_compiled(*, company, role, filename, edits_applied, fit_score,
  keywords_total, keywords_matched) -> str` — creates `data/` if needed,
  appends one line, returns the id. Open in append mode with
  `encoding="utf-8"`; one `json.dumps` per line, `\n` terminated. A
  `threading.Lock` around the append matches the concurrency posture of
  `jobs.py` (single process, request threads).
- `read_applications() -> list[dict]` — parse + fold events as described.

CLI half (`python -m backend.app.tracker <subcommand>` via
`if __name__ == "__main__":` + `argparse`):

- `list` — one line per application: short id (first 8 chars), date,
  company, role, fit score, latest outcome status (default `compiled`).
- `outcome <id-prefix|latest> <status> [--note "..."]` — appends an outcome
  event. `latest` targets the most recently compiled application; an id
  prefix must match exactly one application or the command errors listing
  the ambiguous matches. Validate `status` against the allowed set.
- `report` — writes `output/applications_report.html`: fully
  self-contained (inline CSS, no external assets, no JS needed), a summary
  funnel (counts per outcome status) and a table of all applications
  (date, company, role, fit score, edits applied, outcome, note). All
  user-derived strings must be HTML-escaped (`html.escape`) — company/role
  originate from LLM output over an untrusted job posting, so treat them as
  untrusted in an HTML context too.

### 2. `backend/app/main.py` — record in `/compile`'s success path

In `compile_resume`, immediately after the PDF is written to
`config.OUTPUT_DIR` (`main.py:181-183`), call `tracker.record_compiled(...)`
with:

- `company=req.company`, `role=req.role`, `filename=filename`,
  `edits_applied=len(req.approved_edits)`.
- Fit numbers: recompute deterministically via
  `compute_fit(_base_resume(), req.keywords)` — `CompileRequest` already
  carries `keywords` (`models.py:80-84`), and `compute_fit` comes from
  `CODEX_TASK_job_fit_score.md`. **Ordering dependency: implement the
  job-fit-score task first.** If for some reason it isn't landed yet, write
  the fit fields as `null` rather than blocking this task on it.
- Wrap the tracker call in `try/except Exception` that logs to stderr and
  continues — a tracking failure must never turn a successful compile into
  a 500; the user's PDF matters more than the log line.

### 3. `.gitignore`

Add `data/` — this file contains the user's personal application history
and must never be committed.

### 4. `README.md`

Short "Tracking applications" section documenting the three CLI commands and
that the report lands at `output/applications_report.html`. Note that
`docker compose run --rm backend python -m backend.app.tracker list` is the
Docker-path equivalent (requires the `./data:/app/data` volume — add it to
`docker-compose.yml` if that task has landed; if not, note it inside
`CODEX_TASK_docker_single_command_setup.md`'s compose block instead so
whichever lands second picks it up).

## Verification

- Unit tests (unittest style, in `backend/tests/`):
  - `record_compiled` then `read_applications` round-trips all fields;
    `DATA_DIR` pointed at a `tempfile.TemporaryDirectory` for every test
    (same isolation pattern `ApiTests` already uses for `OUTPUT_DIR` — see
    PLAN.md §9.1; do not let tests write a real `data/`).
  - An `outcome` event updates the folded status; two outcomes → last wins.
  - A malformed line in the file is skipped, remaining lines still parse.
  - `outcome` CLI id-prefix resolution: unique prefix works, ambiguous
    prefix errors.
  - Report generation: output contains the expected rows and
    `html.escape`d content — feed a company name like
    `<script>alert(1)</script>` and assert it appears escaped.
- API test: extend the existing mocked `/compile` success test to assert
  exactly one `compiled` line is appended (into the temp `DATA_DIR`), and
  that a tracker write failure (monkeypatch `record_compiled` to raise)
  still returns the PDF response.
- `python -m unittest discover -s backend/tests -v` passes; a test run
  leaves no `data/` directory in the real repo.
