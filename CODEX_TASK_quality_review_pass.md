# Task: a second, quality-focused review pass on tailored edits and letter paragraphs

## Why this task exists (read this first)

PLAN.md §18's positioning review named this the highest-leverage strategic
move available: this codebase already has a rigorous *truth* check
(`resume_parser.validate_edit`, `letter.validate_letter_paragraph`) that
deterministically rejects any fabricated entity, number, or wording. It has
nothing that checks *quality* — an edit can be perfectly traceable to the
original bullet and still be a weak rewrite (passive voice, unquantified,
generic). Popular public alternatives (ai-job-search's drafter-reviewer
pattern) already do a second critique pass; this closes that gap without
copying their approach, because their reviewer is LLM-checking-LLM with no
deterministic backstop, while this task's reviewer only ever *adds a
suggestion a human can see* — it cannot change what gets compiled, full
stop. That distinction is the entire point: extend the review screen,
never bypass it (§5.3).

**Hard constraint, not a suggestion:** a quality note is informational only.
It must never disable, hide, or auto-modify an edit, never change
`traceable`, never block selection, and never change what `/compile` or
`/cover-letter/compile` accept. If a quality-review call fails outright,
the job still completes normally with no quality notes — this is a
nice-to-have layer, not a new failure mode for the pipeline. Model it after
how `_run_cover_letter_job` already treats a single paragraph's grounding
check failure as a flag, not a job failure (`main.py:124-134`), and how
`score_new()` skips a single posting's scoring failure without aborting
the run (`discovery.py:293-320`).

## Scope

`backend/app/llm.py` (two new functions sharing one private helper),
`backend/app/models.py` (two new small models, two new fields on existing
ones), `backend/app/main.py` (call sites in the two existing background-job
functions only), `extension/popup.js` + `popup.css` (render the new field).
No changes to `resume_parser.py`, `letter.py`, `/compile`, or
`/cover-letter/compile` — the safety-critical validation path is untouched.
No changes to `jobs.py` — it serializes whatever fields `ReviewedEdit`/
`ReviewedParagraph` have via `.model_dump()`/`.model_validate()`
(`jobs.py`'s `_serialize`/`_deserialize`), so a new field with a default
round-trips through Redis automatically.

## Backend changes

### 1. `backend/app/models.py`

Add near `KeywordMatch`/`FitReport`:

```python
class QualitySuggestion(BaseModel):
    index: int = Field(ge=0)
    note: str = Field(min_length=1, max_length=300)


class QualityReviewResponse(BaseModel):
    suggestions: list[QualitySuggestion] = Field(default_factory=list, max_length=20)
```

Add `quality_notes: list[str] = Field(default_factory=list)` to both
`ReviewedEdit` and `ReviewedParagraph`. Both already flow through
`TailorStatusResponse`/`CoverLetterStatusResponse` unchanged (they embed
`list[ReviewedEdit]`/`list[ReviewedParagraph]` directly), so no other
model needs editing.

### 2. `backend/app/llm.py`

Add a private shared helper plus two public functions, placed after
`draft_cover_letter` (which currently ends the model-calling section
before the `_plain_latex_text`/prep helpers begin):

```python
def _index_notes(response: QualityReviewResponse, count: int) -> dict[int, list[str]]:
    notes: dict[int, list[str]] = {}
    for suggestion in response.suggestions:
        if not 0 <= suggestion.index < count:
            continue
        notes.setdefault(suggestion.index, []).append(suggestion.note)
    return notes


def review_edit_quality(edits: list[ReviewedEdit]) -> dict[int, list[str]]:
    reviewable = [(index, edit) for index, edit in enumerate(edits) if edit.traceable]
    if not reviewable:
        return {}
    listing = "\n".join(f"{index}: {edit.new_text}" for index, edit in reviewable)
    system = """You review resume bullet rewrites for writing quality only — never for
truthfulness; that is already verified separately. For each bullet, note only concrete,
actionable weaknesses: passive voice, vague claims with no quantification where a number
would plausibly exist, generic filler phrasing, or a weak/buried action verb. Skip a bullet
entirely if it has no real weakness — do not manufacture a note to have something to say.
Never suggest adding any fact, tool, number, or claim; you may only comment on phrasing of
what is already there. Keep each note to one short sentence. Return at most 12 notes."""
    prompt = f"""BULLETS (index: text)
{listing}"""
    wire_schema = {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "note": {"type": "string"},
                    },
                    "required": ["index", "note"],
                },
            }
        },
        "required": ["suggestions"],
    }
    result = _call(system=system, prompt=prompt, response_model=QualityReviewResponse, format_schema=wire_schema)
    assert isinstance(result, QualityReviewResponse)
    return _index_notes(result, len(edits))


def review_letter_quality(paragraphs: list[str]) -> dict[int, list[str]]:
    if not paragraphs:
        return {}
    listing = "\n".join(f"{index}: {text}" for index, text in enumerate(paragraphs))
    system = """You review cover-letter paragraphs for writing quality only — never for
truthfulness; that is already verified separately. For each paragraph, note only concrete,
actionable weaknesses: generic phrases ("hardworking team player", "passionate about"),
restating the resume instead of connecting it to the role, or a flat/impersonal opening.
Skip a paragraph entirely if it has no real weakness — do not manufacture a note. Never
suggest adding any fact, employer, number, or claim; comment only on phrasing of what is
already there. Keep each note to one short sentence. Return at most 8 notes."""
    prompt = f"""PARAGRAPHS (index: text)
{listing}"""
    wire_schema = {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "note": {"type": "string"},
                    },
                    "required": ["index", "note"],
                },
            }
        },
        "required": ["suggestions"],
    }
    result = _call(system=system, prompt=prompt, response_model=QualityReviewResponse, format_schema=wire_schema)
    assert isinstance(result, QualityReviewResponse)
    return _index_notes(result, len(paragraphs))
```

Import `QualityReviewResponse` (and, for the `review_edit_quality`
type hint, `ReviewedEdit`) into `llm.py`'s existing `from .models import
(...)` block.

Note `review_edit_quality` only sends `edit.new_text` for edits where
`edit.traceable` is true — an edit that's already unselectable for
factual reasons doesn't need a second kind of flag layered on top; that
would confuse "this is blocked" with "this is weak," which is exactly the
distinction the UI change below has to keep visually separate. Neither
function receives the job posting or the base resume — this call only
ever sees text the safety layer has already approved, so it's the one
model call in this codebase with no untrusted-input surface at all (no
`<job_posting>`-style delimiting needed, unlike every other function in
this file).

### 3. `backend/app/main.py`

Import `review_edit_quality, review_letter_quality` into the existing
`from .llm import (...)` line (`main.py:16`).

In `_run_tailor_job` (`main.py:91-110`), after building `reviewed`
(`main.py:104`) and before the final `update_job` call:

```python
        reviewed = _review_edits(source, proposals, keyword_terms)
        try:
            quality_notes = review_edit_quality(reviewed)
        except (LLMError, ValidationError) as exc:
            print(f"warning: quality review failed: {exc}", file=sys.stderr)
            quality_notes = {}
        if quality_notes:
            reviewed = [
                edit.model_copy(update={"quality_notes": quality_notes.get(index, [])})
                for index, edit in enumerate(reviewed)
            ]
        update_job(job_id, status="done", analysis=analysis, edits=reviewed, fit=fit)
```

`sys` is already imported at `main.py:2`. Use `.model_copy(update=...)`,
not direct attribute assignment — this matches the existing pattern in
`llm.py`'s `_prepare_bullet_edits` (`edit.model_copy(update={"new_text": escaped})`)
rather than introducing a second style for mutating a `ReviewedEdit`.

In `_run_cover_letter_job` (`main.py:113-139`), after the `paragraphs`
list is fully built (after the existing `for paragraph in draft.paragraphs`
loop, `main.py:124-134`) and before `update_job`:

```python
        try:
            quality_notes = review_letter_quality([paragraph.text for paragraph in paragraphs])
        except (LLMError, ValidationError) as exc:
            print(f"warning: quality review failed: {exc}", file=sys.stderr)
            quality_notes = {}
        if quality_notes:
            paragraphs = [
                paragraph.model_copy(update={"quality_notes": quality_notes.get(index, [])})
                for index, paragraph in enumerate(paragraphs)
            ]
        update_job(job_id, status="done", paragraphs=paragraphs)
```

Optional, only if it falls out naturally: update the `step` text before
each quality-review call (e.g. `"Reviewing phrasing quality…"`) the same
way `main.py:96-101` already updates `step` between pipeline stages — skip
it if it complicates the diff, since the whole quality pass is fast
relative to edit generation and the UI already shows *something* is
happening.

## Extension changes

### 4. `extension/popup.js`

In `renderResults()`'s edit-card loop (`popup.js`, the block building
`card` from `state.edits.map(...)`, currently ending with the
`if (!edit.traceable) { ...flag... }` block), add directly after it:

```js
    if (edit.quality_notes?.length) {
      const suggestion = document.createElement("div");
      suggestion.className = "warning";
      suggestion.textContent = edit.quality_notes.join(" · ");
      card.append(suggestion);
    }
```

In `renderLetter()`'s paragraph-card loop, add the equivalent block after
the existing `if (paragraph.issues?.length) { ...flag... }` block, reading
`paragraph.quality_notes` instead.

Reuse the existing `warning` class (`popup.css`'s `.warning` rule, already
used for the fit-score high-missing-keyword note) rather than adding a new
CSS class — it's already the established "heads up, not blocking" amber
treatment in this popup, visually distinct from `.flag`'s red "blocked"
treatment, and reusing it means no CSS changes are needed at all. Do not
reuse `.flag` — a quality note is never a safety block and must not look
like one.

No changes to `background.js` — `quality_notes` arrives embedded in the
existing `/tailor/status` and `/cover-letter/status` polling responses
exactly the way `fit` did (see `CODEX_TASK_job_fit_score.md`'s identical
note that `background.js` needs no changes because `pollStoredJob` already
stores the entire response JSON verbatim).

## Verification

- `backend/tests/test_llm.py`: `review_edit_quality`/`review_letter_quality`
  — mock `_call` and assert (a) the wire schema and system prompt forbid
  adding new claims, (b) an out-of-range index from a malformed model
  response is dropped rather than raising, (c) `review_edit_quality` never
  includes a non-`traceable` edit's text in the prompt sent to `_call`
  (assert on `call_args`), (d) an empty input list returns `{}` without
  calling `_call` at all.
- `backend/tests/test_api.py`: extend the existing mocked `/tailor/start`
  → `/tailor/status` and `/cover-letter/start` → `/cover-letter/status`
  tests with a mocked `review_edit_quality`/`review_letter_quality`
  returning notes, and assert `quality_notes` appears on the right edit/
  paragraph in the status response. Add one test per job type where the
  quality-review mock raises `LLMError` and assert the job still reaches
  `status="done"` with empty `quality_notes` — this is the test that
  actually proves the "never a new failure mode" constraint holds, not
  just the happy path.
- `python -m unittest discover -s backend/tests -v` passes.
- `node --check extension/popup.js` passes; `node extension/tests/background.test.js`
  passes unmodified (this task doesn't touch `background.js`).
- Manual: run a real tailor pass and a real cover-letter draft; confirm
  quality notes render in the existing amber style, distinct from any red
  flagged-edit styling, and confirm an edit that's both flagged (unsafe)
  and would otherwise have a quality note only shows the flag (per the
  `traceable`-only filter in `review_edit_quality`) — there should be no
  bullet showing both a red flag and an amber suggestion simultaneously.
