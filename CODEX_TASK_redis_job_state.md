# Task: back tailor/letter job state with Redis instead of an in-process dict

## Why this task exists (read this first)

`backend/app/jobs.py` currently holds every in-flight tailor/cover-letter
job in a plain Python `dict` (`_jobs`, `jobs.py:24`) behind a
`threading.Lock`. That state is lost on any backend restart, and can't be
shared if the backend ever runs as more than one worker process. PLAN.md
§14.4 calls this out as "the one legitimate infra rep already motivated by
this repo's own code" — unlike Kubernetes/Databricks (rejected there as
solving distributed-scale problems this single-user, single-container
project doesn't have), this fixes a real, already-present limitation. It's
optional/rep-driven, not urgent — nothing is currently broken by the
in-memory version at this project's scale — but the user asked for it to
be planned and spec'd now (PLAN.md §16.2).

**Be upfront about the real cost this adds**, because it's a genuine
tradeoff, not a free win: this introduces the project's first hard runtime
dependency on an external service. Every other piece of local
infrastructure here (Ollama, MiKTeX/`pdflatex`) was already a prerequisite
before this task; Redis is new. Docker Compose users get it for free (a
`redis` service added to `docker-compose.yml`, §2 below). **Native Windows
users (this repo's actual primary dev environment — see README's "Native
one-time setup") do not get a first-party Redis build** — Microsoft's old
port is unmaintained and upstream Redis dropped Windows support years ago.
The pragmatic local option, since Docker Desktop is already a documented
prerequisite for this repo's packaging story, is `docker run -p
127.0.0.1:6379:6379 redis:7-alpine` — one command, no compose file needed
for native dev. Document this in README (§4 below) rather than silently
assuming it away.

**Unit tests do not get a real-Redis dependency.** Following this repo's
existing convention of mocking the external service in fast unit tests and
reserving the real thing for a manual smoke script (`llm.py`'s Ollama
calls are mocked in `test_api.py`/`test_llm.py`; `ollama_smoke.py` is the
separate manual script that hits real Ollama), this task uses `fakeredis`
(an in-memory, pure-Python Redis-protocol stand-in) for
`python -m unittest discover` and CI, and adds a new manual
`backend/tests/redis_smoke.py` for the real thing. **This means no CI
workflow changes are needed** — `backend-tests` in `.github/workflows/ci.yml`
keeps running exactly as today, fully mocked, no new service container.

## Scope

`backend/app/jobs.py` (full rewrite of its storage layer; the public
`create_job`/`get_job`/`update_job` function signatures and the `TailorJob`
dataclass shape are unchanged, so `main.py` needs zero changes), plus
`backend/app/config.py`, `backend/requirements.txt`, `docker-compose.yml`,
`README.md`, and new/updated tests. No changes to `main.py`, the extension,
or any other endpoint's behavior — this is purely a storage-backend swap
behind an already-stable internal API.

## Backend changes

### 1. `backend/app/config.py`

Add near the other service-connection settings (after the Ollama block,
`config.py:22-24`):

```python
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
JOB_STATE_TTL_SECONDS = int(os.environ.get("JOB_STATE_TTL_SECONDS", "3600"))
```

One hour is deliberately generous: the extension polls every 4 seconds
while a job is `running` and removes its local job-id storage the moment
a job reaches a terminal state (`background.js`'s `pollStoredJob`), so a
real session finishes in well under a minute. The TTL exists only to stop
an abandoned/crashed job from occupying Redis forever, not to bound normal
usage.

### 2. `backend/app/jobs.py` — full rewrite

```python
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal

import redis

from . import config
from .models import ExtractKeywordsResponse, FitReport, ReviewedEdit, ReviewedParagraph


JobStatus = Literal["running", "done", "error"]


@dataclass
class TailorJob:
    kind: Literal["tailor", "letter"] = "tailor"
    status: JobStatus = "running"
    step: str = "Extracting role requirements…"
    analysis: ExtractKeywordsResponse | None = None
    edits: list[ReviewedEdit] = field(default_factory=list)
    fit: FitReport | None = None
    paragraphs: list[ReviewedParagraph] = field(default_factory=list)
    error: str | None = None


_lock = threading.Lock()
_redis_client: redis.Redis | None = None


def _client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis_client


def _key(job_id: str) -> str:
    return f"resume-tailor:job:{job_id}"


def _serialize(job: TailorJob) -> str:
    return json.dumps(
        {
            "kind": job.kind,
            "status": job.status,
            "step": job.step,
            "analysis": job.analysis.model_dump() if job.analysis else None,
            "edits": [edit.model_dump() for edit in job.edits],
            "fit": job.fit.model_dump() if job.fit else None,
            "paragraphs": [paragraph.model_dump() for paragraph in job.paragraphs],
            "error": job.error,
        }
    )


def _deserialize(raw: str) -> TailorJob:
    payload = json.loads(raw)
    return TailorJob(
        kind=payload["kind"],
        status=payload["status"],
        step=payload["step"],
        analysis=ExtractKeywordsResponse.model_validate(payload["analysis"]) if payload["analysis"] else None,
        edits=[ReviewedEdit.model_validate(edit) for edit in payload["edits"]],
        fit=FitReport.model_validate(payload["fit"]) if payload["fit"] else None,
        paragraphs=[ReviewedParagraph.model_validate(p) for p in payload["paragraphs"]],
        error=payload["error"],
    )


def create_job(kind: Literal["tailor", "letter"] = "tailor") -> str:
    job_id = uuid.uuid4().hex
    step = "Drafting a grounded cover letter…" if kind == "letter" else "Extracting role requirements…"
    job = TailorJob(kind=kind, step=step)
    _client().set(_key(job_id), _serialize(job), ex=config.JOB_STATE_TTL_SECONDS)
    return job_id


def get_job(job_id: str) -> TailorJob | None:
    raw = _client().get(_key(job_id))
    return _deserialize(raw) if raw is not None else None


def update_job(job_id: str, **fields) -> None:
    with _lock:
        job = get_job(job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        _client().set(_key(job_id), _serialize(job), ex=config.JOB_STATE_TTL_SECONDS)
```

Notes for whoever implements this:

- `_lock` now only serializes the read-modify-write in `update_job` within
  this one Python process — it does not make cross-process updates atomic.
  That's fine: exactly one background thread ever calls `update_job` for a
  given `job_id` (see `_run_tailor_job`/`_run_cover_letter_job` in
  `main.py`), so there is no real concurrent-writer scenario to protect
  against yet. Don't add distributed locking for a race that doesn't exist.
- `redis.Redis.from_url(...)` does not connect eagerly — the first real
  network call happens on the first `.get()`/`.set()`. Don't add a
  connection-check at import time; let a genuinely unreachable Redis
  surface as whatever `redis-py` raises (`redis.exceptions.ConnectionError`),
  the same way an unreachable Ollama already surfaces as an unhandled
  exception inside `_run_tailor_job`'s broad `except Exception` guard in
  `main.py` and turns the job into a clean `error` state.
- `_key`'s `resume-tailor:` prefix exists so this Redis instance can safely
  be shared with something else later without key collisions — it isn't
  solving a problem that exists today, it's just cheap namespacing on a
  key you're writing anyway.

### 3. `backend/requirements.txt`

Add two lines:

```
redis
fakeredis
```

`fakeredis` is a real (non-dev-only) requirement here because this repo
has one `requirements.txt`, not a split prod/dev file — that's an existing
convention, not something to change as part of this task.

## Test changes

### 4. `backend/tests/test_api.py`

In `setUp` (`test_api.py:19-27`), patch the job storage to an isolated
in-memory Redis double per test, so tests never touch a real Redis and
never leak job state between tests:

```python
import fakeredis
from backend.app import jobs
...
    def setUp(self):
        ...
        self.jobs_client_patcher = patch.object(
            jobs, "_client", return_value=fakeredis.FakeRedis(decode_responses=True)
        )
        self.jobs_client_patcher.start()
        self.addCleanup(self.jobs_client_patcher.stop)
```

No other change to `test_api.py` should be necessary — every existing
`/tailor/start` + `/tailor/status` / `/cover-letter/start` +
`/cover-letter/status` test already exercises `create_job`/`get_job`/
`update_job` through the real HTTP endpoints (`test_api.py:176-339`); they
should pass unmodified once storage is backed by the patched fake client.

### 5. New file: `backend/tests/test_jobs.py`

Unit-level coverage for the serialization round trip, independent of the
HTTP layer, using `fakeredis` directly (same style as `test_discovery.py`'s
`setUp`/`tearDown` pattern):

- `create_job` + `get_job` round-trips a freshly created job with default
  fields.
- `update_job` with `analysis`/`edits`/`fit`/`paragraphs` set, then
  `get_job`, confirms every nested Pydantic field round-trips correctly
  (this is the part actually worth testing — `ReviewedEdit`/`FitReport`/
  `ReviewedParagraph` are the fields most likely to break serialization).
- `update_job` on an unknown `job_id` is a silent no-op (matches the old
  in-memory behavior at `jobs.py`'s current `update_job`).
- `get_job` on an unknown `job_id` returns `None`.
- A job's Redis key carries the configured TTL (`fakeredis` supports
  `.ttl()`; assert it's `> 0` and `<= config.JOB_STATE_TTL_SECONDS`).

### 6. New file: `backend/tests/redis_smoke.py`

Manual smoke test (not run by CI, matching `ollama_smoke.py`/
`http_smoke.py`'s existing precedent — add one line to those two files'
module docstrings or README's "Verification" section listing this as a
third manual smoke script). Requires a real reachable `REDIS_URL`
(defaults to `redis://127.0.0.1:6379/0`, same as the app). It should:

1. Create a job via `jobs.create_job()`.
2. Call `jobs.update_job(...)` with representative `analysis`/`fit` data.
3. Force a fresh connection (reset `jobs._redis_client = None`, simulating
   "the backend process restarted") and confirm `jobs.get_job(job_id)`
   still returns the updated state — this is the actual property being
   tested here (state survives a process restart), which `fakeredis`-backed
   unit tests can't prove since they run in the same process.
4. Print a clear pass/fail summary, matching the other smoke scripts'
   style.

### 7. `README.md`

In "Verification" (`README.md:84-103`), add `redis_smoke.py` to the list
of manual smoke tests and describe what it proves (state survives a
simulated backend restart). In "Native one-time setup" (`README.md:32-70`),
add Redis as a new prerequisite with the `docker run -p
127.0.0.1:6379:6379 redis:7-alpine` one-liner for native/non-compose dev,
and note `REDIS_URL` in `backend/.env` if a different host/port is used.
Add `REDIS_URL=redis://127.0.0.1:6379/0` (commented, matching the existing
default) to `backend/.env.example`'s optional-overrides block.

## Docker changes

### 8. `docker-compose.yml`

Add a `redis` service and wire the backend to it:

```yaml
services:
  backend:
    # ...unchanged...
    environment:
      CONFIG_REPO_ROOT: /config
      OLLAMA_MODEL: qwen2.5:7b-instruct
      REDIS_URL: redis://redis:6379/0
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    restart: unless-stopped

volumes:
  ollama-models:
```

No host port mapping for `redis` — only the `backend` service inside the
compose network needs to reach it, and this repo already deliberately
keeps everything except the backend's own `127.0.0.1:8765` off the host
network (see `docker-compose.yml`'s existing `backend.ports` binding and
PLAN.md §3.2's localhost-exposure stance). No named volume for Redis data
either — job state is intentionally ephemeral (TTL'd, request-scoped), not
something that needs to survive a `docker compose down`.

## Verification

- `python -m unittest discover -s backend/tests -v` passes with no real
  Redis running (proves the `fakeredis` patching actually isolates unit
  tests from the real service).
- `python backend/tests/redis_smoke.py` against a real local Redis
  (`docker run -p 127.0.0.1:6379:6379 redis:7-alpine`) passes and
  specifically demonstrates state surviving a simulated restart.
- `docker compose build && docker compose up` brings up both services;
  `docker compose run --rm backend python -c "from backend.app import
  jobs; print(jobs.create_job())"` succeeds without a connection error,
  confirming `REDIS_URL: redis://redis:6379/0` resolves inside the compose
  network.
- Manual: run a real tailor job end-to-end through the extension, then
  restart the backend process mid-poll (native: kill and re-run
  `start_backend.cmd`; Docker: `docker compose restart backend`) — before
  this task the popup would be stuck polling an unknown `job_id` forever;
  after this task, confirm whether the in-flight job's state actually
  survives (it should, since Redis itself isn't restarted) — this is the
  concrete behavior change this task exists to deliver.
