# Task: deterministic job-fit score — show keyword coverage before/without tailoring

## Why this task exists (read this first)

The user's stated goal (2026-07-18) is to improve the odds of passing
automated/AI resume screening (ATS keyword matching). The pipeline already
extracts a structured keyword list from every posting
(`ExtractKeywordsResponse.keywords`, each with `category` and `importance` —
see `backend/app/models.py:6-10`) and already has a resume-entity matcher
(`resume_parser.py`'s `_known_entities`, `_phrase_present`, `_word_tokens`).
What's missing is putting those two together: nothing today tells the user
**how well the resume covers the posting's keywords**, which keywords are
missing entirely (and therefore can never be tailored in — §2 of PLAN.md
forbids adding skills), and which are present but could be surfaced better.

This is a **fully deterministic** feature — zero additional model calls, no
new safety surface, no change to what edits are allowed. It reuses the exact
matching logic the safety validator already trusts.

## Scope

Backend: one new module, small edits to `models.py`, `jobs.py`, `main.py`.
Extension: `popup.js`/`popup.html`/`popup.css` rendering only.
`extension/background.js` needs **no changes** — `pollTailorJob` at
`background.js:78-79` stores the entire `/tailor/status` JSON response into
`chrome.storage`, so a new response field flows to the popup automatically.
Do not change `resume_parser.py`'s validation logic; only *call* its helpers.

## Backend changes

### 1. New file: `backend/app/fit.py`

```python
from pydantic import BaseModel, Field

from .models import Keyword
from .resume_parser import _phrase_present  # same-package private import is fine here


class KeywordMatch(BaseModel):
    term: str
    category: str
    importance: str
    matched: bool


class FitReport(BaseModel):
    score: int = Field(ge=0, le=100)          # weighted coverage percent
    matched: list[KeywordMatch] = Field(default_factory=list)
    missing: list[KeywordMatch] = Field(default_factory=list)


def compute_fit(resume_source: str, keywords: list[Keyword]) -> FitReport: ...
```

Implementation rules for `compute_fit`:

- A keyword counts as **matched** if `_phrase_present(resume_source,
  keyword.term)` is true against the **whole `resume.tex` source**.
  `_phrase_present` already tokenizes via `_word_tokens`, which strips LaTeX
  (`_plain_latex`) and applies the alias/singular-plural normalization in
  `_token_variants`' spirit — whole-resume matching is deliberate here
  (unlike `validate_edit`'s bullet-local grounding): the question "does this
  resume mention Kubernetes anywhere" is resume-global, and Education/
  coursework mentions legitimately count for ATS purposes.
- Weighted score: weight each keyword by importance (`high`=3, `medium`=2,
  `low`=1); `score = round(100 * matched_weight / total_weight)`. If the
  keyword list is empty, return `score=0` with empty lists (don't divide by
  zero).
- Sort both output lists by importance (high first), then term. This is
  presentation-stable so the popup doesn't need its own sorting.
- Keep `category`/`importance` as plain `str` in `KeywordMatch` (copied from
  the `Keyword`) so this module doesn't duplicate the Literal types.

### 2. `backend/app/models.py`

Add `fit: FitReport | None = None` to `TailorStatusResponse`. Import from
`.fit`. Watch for a circular import: `fit.py` imports `Keyword` from
`models.py`, so `models.py` cannot import `fit.py` at module top if that
creates a cycle — if it does, move `FitReport`/`KeywordMatch` **into
`models.py`** instead and have `fit.py` import them from there. Either
placement is acceptable; no cycle is not.

### 3. `backend/app/jobs.py`

Add `fit: "FitReport | None" = None` to the `TailorJob` dataclass (same
pattern as the existing `analysis` field).

### 4. `backend/app/main.py`

In `_run_tailor_job` (`main.py:81-94`), immediately after
`analysis = extract_keywords(job_text)`:

```python
fit = compute_fit(_base_resume(), analysis.keywords)
update_job(job_id, step="Drafting grounded resume edits…", analysis=analysis, fit=fit)
```

(i.e. fold `fit` into the existing `update_job` call — this makes the score
visible via polling *while the slower edit-generation step is still
running*, which matters because generate_edits is the ~minute-long part.)

In `tailor_status`, add `fit=job.fit` to the `TailorStatusResponse`
construction.

Note `_base_resume()` raises `HTTPException` if `resume.tex` is missing —
inside the worker thread that would be caught by the broad `except Exception`
guard and turn the job into a clean error state, which is acceptable; no
special handling needed.

## Extension changes (`popup.js`, `popup.html`, `popup.css`)

In `renderResults` (`popup.js:26-61`), when `state.analysis.fit` exists
(`applyTailorState` must copy `tailorState.fit` into `state.analysis` at
`popup.js:69-73`):

1. Show a headline line above the keyword chips: `Fit: 78% — 9 of 12
   keywords covered` (weighted score plus plain matched-count).
2. Render the existing keyword chips (`popup.js:30-32`) in two visually
   distinct states: matched (current chip style) and missing (muted/outlined
   style, new CSS class `chip-missing`). Use the `fit.matched`/`fit.missing`
   lists as the render source instead of `state.analysis.keywords` when fit
   data is present; fall back to the current undifferentiated rendering when
   it isn't (old stored results won't have `fit`).
3. Under the missing chips, when any missing keyword has
   `importance === "high"`, show one plain-language line:
   > Missing keywords cannot be added by tailoring — this tool never invents
   > experience. A low fit score means this posting may not be worth the
   > application, or the resume needs real (human) updating.
   This sentence is the actual product value: it tells the user *before*
   they invest review effort whether the posting is a realistic target.

Optional (nice-to-have, skip if fiddly): since `fit` arrives in the status
response while the job is still `running`, `applyTailorState`'s running
branch could show `Fit: 78% — drafting edits…` in the progress text. Do not
restructure the storage/polling flow to achieve this; it's only worth doing
if it falls out naturally.

**Coordination note:** `CODEX_TASK_edit_budget_and_ux.md` Task 2 also edits
`renderResults` (the flagged-edit summary). These changes are compatible —
fit renders above the keyword list, that summary sits above the edit cards —
but if both tasks are done in one pass, apply them together rather than
having one clobber the other.

## Verification

- Unit tests for `compute_fit` in `backend/tests/` (new or existing test
  module, matching the current unittest style):
  - A keyword present verbatim in `resume.tex` → matched.
  - A keyword absent → missing, and the weighted score reflects importance
    weighting (e.g. one missing `high` costs more than one missing `low`).
  - Alias behavior: whatever `_phrase_present` already normalizes (e.g.
    "JS" vs "JavaScript" via `_token_variants`' alias map) — assert the
    behavior it actually has, don't assert an alias the helper doesn't
    implement.
  - Empty keyword list → `score=0`, no crash.
- API test: `/tailor/status` for a completed job includes a `fit` object
  (extend the existing mocked-LLM tailor-job test in `test_api.py`).
- `python -m unittest discover -s backend/tests -v` passes;
  `node extension/tests/background.test.js` passes (should be untouched);
  `node --check extension/popup.js` passes.
- Manual: run a real tailor pass; confirm the fit line and chip styling
  appear, and that a posting full of technologies not on the resume shows a
  low score with the plain-language warning.
