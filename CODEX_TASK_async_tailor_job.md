# Task: replace in-extension analysis with a backend-owned polled job

## Why this task exists (read this first)

The extension currently runs the job-posting analysis (`/extract-keywords`
then `/generate-diff`, which round-trip to a local Ollama model and take
roughly a minute combined) either directly inside the popup's own script, or
— after a prior attempt to fix it — inside `extension/background.js`'s
service worker using an in-memory `let tailorState` variable plus
`chrome.runtime.sendMessage` broadcasts.

Both approaches are broken for the same underlying reason: **a Manifest V3
extension service worker is not a persistent process.** Chrome is free to
terminate `background.js` and restart it whenever it judges the worker idle,
including in the middle of an in-flight `fetch()`. When that happens, every
plain JS variable in that file (like `tailorState`) is wiped, and there is no
way to resume the killed async function. A ~1 minute local-model round trip
falls squarely inside the window where this happens in practice — this is
why the user still loses the run when switching tabs, even after the
background-worker fix.

**The correct fix is to stop doing the long-running work inside the browser
extension at all.** The FastAPI backend (`backend/app/main.py`) already runs
as a normal, persistent local Python process (started via
`start_backend.cmd`/`start_backend.ps1`) — it has none of the service-worker
lifecycle problems. Move the actual analysis work there, behind a
create-job/poll-job API, and have the extension only track a small `job_id`
in `chrome.storage.local` (which survives service worker restarts) plus a
`chrome.alarms` timer (which also survives restarts and is the
Chrome-documented mechanism for driving periodic work from an MV3 service
worker) to poll it. Do not use an in-memory variable or `sendMessage`
broadcasts as the source of truth for job state — `chrome.storage` is the
only thing here that's actually durable.

## Scope

Three files change on the backend, three on the extension. Do not change
`resume.tex`, `resume.cls`, or `backend/app/resume_parser.py` — those are
unrelated and already correct.

## Backend changes

### 1. New file: `backend/app/jobs.py`

An in-process job store. Since this is a single local user with one backend
process (not a multi-worker deployment), a simple `dict` guarded by a
`threading.Lock` is sufficient — no database, no Redis, no persistence to
disk needed.

```python
import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal

from .models import ExtractKeywordsResponse, ReviewedEdit

JobStatus = Literal["running", "done", "error"]


@dataclass
class TailorJob:
    status: JobStatus = "running"
    step: str = "Extracting role requirements…"
    analysis: ExtractKeywordsResponse | None = None
    edits: list[ReviewedEdit] = field(default_factory=list)
    error: str | None = None


_jobs: dict[str, TailorJob] = {}
_lock = threading.Lock()


def create_job() -> str:
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = TailorJob()
    return job_id


def get_job(job_id: str) -> TailorJob | None:
    with _lock:
        return _jobs.get(job_id)


def update_job(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
```

(No TTL/cleanup logic needed — this is a single-user local tool restarted
often enough via `start_backend`. Don't add complexity here that isn't
asked for.)

### 2. `backend/app/models.py` — add request/response models

Add near the existing `GenerateDiffRequest`/`GenerateDiffResponse`:

```python
class StartTailorRequest(BaseModel):
    job_text: str = Field(min_length=50)


class StartTailorResponse(BaseModel):
    job_id: str


class TailorStatusResponse(BaseModel):
    status: Literal["running", "done", "error"]
    step: str = ""
    company: str | None = None
    role: str | None = None
    keywords: list[Keyword] = Field(default_factory=list)
    edits: list[ReviewedEdit] = Field(default_factory=list)
    error: str | None = None
```

### 3. `backend/app/main.py` — new endpoints, run the work in a thread

Replace what the extension used to do (call `/extract-keywords` then
`/generate-diff` itself) with a single backend-orchestrated job. Reuse the
existing `generate_edits`/`extract_keywords` functions from `llm.py` and the
existing validation loop already in the current `/generate-diff` handler —
don't rewrite that validation logic, just move where it's triggered from.

```python
import threading

from .jobs import TailorJob, create_job, get_job, update_job
from .models import StartTailorRequest, StartTailorResponse, TailorStatusResponse


def _run_tailor_job(job_id: str, job_text: str) -> None:
    try:
        analysis = extract_keywords(job_text)
        update_job(job_id, step="Drafting grounded resume edits…")
        source = _base_resume()
        proposals = generate_edits(job_text, analysis.keywords, source)

        keyword_terms = [k.term for k in analysis.keywords if k.category == "technology"]
        reviewed: list[ReviewedEdit] = []
        seen: set[tuple[str, str, int | None]] = set()
        for proposal in proposals:
            key = (proposal.target.section, proposal.target.anchor, proposal.target.item_index)
            try:
                original, issues = validate_edit(source, proposal, keyword_terms)
            except ResumeEditError as exc:
                original, issues = "", [str(exc)]
            if key in seen:
                issues.append("duplicate target")
            seen.add(key)
            reviewed.append(ReviewedEdit(
                **proposal.model_dump(), original_text=original,
                traceable=not issues, issues=list(dict.fromkeys(issues)),
            ))

        update_job(job_id, status="done", analysis=analysis, edits=reviewed)
    except (LLMError, ValidationError) as exc:
        update_job(job_id, status="error", error=str(exc))
    except Exception as exc:  # last-resort guard so a job never hangs at "running" forever
        update_job(job_id, status="error", error=f"Unexpected error: {exc}")


@app.post("/tailor/start", response_model=StartTailorResponse)
def start_tailor(req: StartTailorRequest, _: None = Depends(require_extension_origin)) -> StartTailorResponse:
    _check_job_size(req.job_text)
    job_id = create_job()
    threading.Thread(target=_run_tailor_job, args=(job_id, req.job_text), daemon=True).start()
    return StartTailorResponse(job_id=job_id)


@app.get("/tailor/status/{job_id}", response_model=TailorStatusResponse)
def tailor_status(job_id: str, _: None = Depends(require_extension_origin)) -> TailorStatusResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return TailorStatusResponse(
        status=job.status, step=job.step,
        company=job.analysis.company if job.analysis else None,
        role=job.analysis.role if job.analysis else None,
        keywords=job.analysis.keywords if job.analysis else [],
        edits=job.edits, error=job.error,
    )
```

Important: `threading.Thread`, not `asyncio.create_task` or
`BackgroundTasks` — the LLM call chain (`llm.py`'s `_call`) uses a
**synchronous** `httpx.Client`, which blocks. Running it via
`BackgroundTasks` or a bare coroutine would block FastAPI's single-threaded
event loop and make `/tailor/status` (and `/health`, `/compile`, everything
else) unresponsive for the full ~1 minute of the job. A real OS thread avoids
that.

You can leave `/extract-keywords` and `/generate-diff` in place unchanged
(harmless, and `/extract-keywords` in particular might still be useful for
manual testing) — just don't have the extension call them directly for the
main flow anymore.

## Extension changes

### 1. `extension/manifest.json`

Add the `alarms` permission:

```json
"permissions": ["activeTab", "scripting", "storage", "downloads", "alarms"],
```

### 2. `extension/background.js` — replace the in-memory job logic

Delete `tailorState`, `broadcastTailorState`, `runTailor`, and the
`START_TAILOR`/`GET_TAILOR_STATE`/`RESET_TAILOR_STATE` message branches added
in the previous (broken) attempt. Replace with:

- `START_TAILOR` handler: extracts the job posting text (keep
  `extractJobPosting(tabId)` as-is, it's fine), POSTs to
  `${backendUrl}/tailor/start`, gets back `{ job_id }`, and writes
  `{ activeJobId: job_id, backendUrl, sharedSecret }` to
  `chrome.storage.local`. Then calls `ext.alarms.create("tailor-poll", {
  periodInMinutes: 1/15 })` (that's a 4-second period — `chrome.alarms`
  cannot fire faster than roughly once per 30 seconds in *production*, but
  Chrome allows shorter periods for unpacked/dev extensions; if the minimum
  enforced period causes overly slow polling in testing, fall back to
  `setTimeout`-chained polling *inside* the alarm handler while the service
  worker happens to be alive, but the alarm itself is what guarantees
  polling resumes after a service-worker restart — don't drop the alarm).
- `ext.alarms.onAlarm.addListener(...)`: if `alarm.name !== "tailor-poll"`,
  ignore. Otherwise read `activeJobId` from `chrome.storage.local`; if
  there's none, clear the alarm and return. Otherwise `GET
  ${backendUrl}/tailor/status/${activeJobId}` and write the full response
  into `chrome.storage.local` under a `tailorResult` key. If
  `status !== "running"`, also clear the alarm (`ext.alarms.clear("tailor-poll")`)
  and clear `activeJobId` — the job is finished, stop polling.
- Remove `RESET_TAILOR_STATE` as a message type; instead, "reset" just means
  clearing `tailorResult`/`activeJobId` from `chrome.storage.local` directly
  (popup can do this itself via `ext.storage.local.remove(...)`, no
  round-trip through background needed for that).
- Leave `COMPILE_AND_DOWNLOAD` exactly as it is — that part was never
  broken, it already correctly runs in the background worker for the single
  short-lived compile request and doesn't need job/polling machinery.

### 3. `extension/popup.js` — read from storage, not from background messaging

- Delete `applyTailorState`'s use of `ext.runtime.onMessage` for
  `TAILOR_STATE` — replace with `ext.storage.onChanged.addListener((changes,
  area) => { if (area === "local" && changes.tailorResult) { ...
  re-render ... } })` so the popup updates live *while open*, and separately,
  on popup load, read `chrome.storage.local.get(["tailorResult",
  "activeJobId"])` directly to pick up wherever things stand — this works
  correctly even if the popup was closed for the entire duration of the job
  and the background worker restarted five times in between, because none of
  the state lived in the worker's memory.
- `tailor()`: unchanged in spirit — still resolves `tabId` and `settings()`,
  still sends `START_TAILOR` to the background worker. The background worker
  now does the extraction + starts the backend job instead of running the
  whole analysis itself.
- Keep the "done"/"error"/"running" UI branching logic and the `hide` calls
  exactly as already fixed (hide `#intro`/`#results`/`#done` uniformly before
  branching) — that part of the previous fix was correct, don't regress it.
- Keep the edit-count summary line (`#edit-summary`) exactly as already
  implemented in `renderResults()` — also correct, don't regress it.

## Verification (don't skip this)

1. `cd backend && python -m pytest` — confirm nothing existing broke.
2. Manually start the backend, load the unpacked extension, open a job
   posting tab, click "Tailor this resume", and **immediately switch to a
   different tab** for the full duration (don't peek). Then switch back or
   reopen the popup after ~90 seconds. Confirm results are present — this is
   the actual scenario that was broken before; it must work now.
3. Also test: start a tailor run, then go to `chrome://extensions`, find the
   Resume Tailor service worker, and manually terminate it (there's usually
   an "Inspect views: service worker" link — closing that devtools/forcing a
   worker restart, or simply waiting ~30s with no extension activity, should
   let Chrome reclaim it). Confirm the job still completes and the popup
   still shows the result afterward — this specifically proves the fix
   doesn't depend on the service worker surviving.
4. Run `node extension/tests/background.test.js` — update/extend it if the
   `COMPILE_AND_DOWNLOAD` message contract changed at all (it shouldn't
   have).
