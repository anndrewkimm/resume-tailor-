# Task: review-gated cover letter generation

## Why this task exists (read this first)

Applications increasingly require a cover letter, and it's the other half of
"pass the automated screen": a letter that names the company, role, and the
posting's actual keywords, grounded in the candidate's real experience. The
pipeline already has everything needed as inputs — posting analysis
(company/role/keywords), the bullet catalog, the async job pattern, the
LaTeX compile path with one-page enforcement, and the background-owned
download lifecycle. This task composes them into a second document type.

**This is the largest of the four planned tasks and the only one that
touches the safety model. Read the grounding-rules section twice before
writing code.** Implement it after the fit-score and tracker tasks — it
consumes `compute_fit`'s sibling helpers and should log to the tracker.

## The safety model for letters (the core design — everything else is plumbing)

A cover letter cannot obey `validate_edit`'s bullet-local token grounding —
prose like "I am excited to apply to NVIDIA because…" is, by construction,
text that appears nowhere in `resume.tex`. Applying the bullet rules would
make every letter impossible; silently dropping all grounding would recreate
the §10 fabrication incident in a new document type. The deliberate middle
ground, to implement exactly:

**Free (never flagged):** connective, motivational, and structural prose —
verbs, opinions, enthusiasm, transitions. This asserts nothing factual about
the candidate's history.

**Whole-resume-grounded (flagged if absent from `resume.tex`):** checked
per paragraph, server-side, in a new `validate_letter_paragraph` function:

1. **Numbers.** Any number in the letter (reuse the regex from
   `resume_parser.py:246`) must appear somewhere in `resume.tex`. Note the
   deliberate difference from bullets: bullet edits require *bullet-local*
   number grounding; letters require *resume-global* grounding, because a
   letter legitimately summarizes across the whole resume ("improved
   accuracy to 50%" may come from any bullet). Company/role strings from the
   analysis are stripped before this check so "NVIDIA" or a role title
   containing a number never false-flags.
2. **Known entities and posting keywords.** For every candidate phrase in
   `_known_entities(resume_source)` plus every posting `keyword.term`: if
   `_phrase_present(paragraph, phrase)` but not
   `_phrase_present(resume_source, phrase)`, flag it. This is the exact
   §10-class defense: the model naming a technology from the posting that
   the resume doesn't have is the fabrication ATS-bait failure mode, and
   it's caught by the same matcher `validate_edit` trusts.
3. **LaTeX safety (hard reject, not just a flag):** same checks as
   `validate_edit` (`resume_parser.py:207-224`) — control characters,
   unbalanced braces, any backslash command at all (letters are plain text;
   unlike bullets, don't even allow `\textbf`), unescaped `%&_#$`.
   Escaping into LaTeX is done by trusted Python code at compile time,
   never by the model.

**The human boundary:** every paragraph renders in the popup as an editable
textarea with its flags shown beneath it. At compile time the final
(possibly user-edited) text is re-validated server-side. LaTeX-safety
failures are always a 422. Grounding failures are a 422 **unless** the
request carries `confirmed_by_user: true`, which the popup sets only via an
explicit checkbox ("I confirm every claim in this letter is true"), unchecked
by default, shown only when flags exist. Rationale: model output defaults to
blocked-when-ungrounded, but a human who typed or vetted a true fact that
happens not to be on the one-page resume (e.g. "I'm a junior at UCSD") is
the legitimate source of truth this project's §5.3 review gate exists to
empower. The server stores nothing about this; it's per-request.

## Backend changes

### 1. `backend/app/models.py`

```python
class LetterParagraph(BaseModel):
    text: str = Field(min_length=1, max_length=1200)

class CoverLetterDraftResponse(BaseModel):        # model wire format
    paragraphs: list[LetterParagraph] = Field(min_length=2, max_length=6)

class ReviewedParagraph(BaseModel):
    text: str
    issues: list[str] = Field(default_factory=list)

class StartCoverLetterRequest(BaseModel):
    job_text: str = Field(min_length=50)
    company: str = Field(default="Company", max_length=120)
    role: str = Field(default="Role", max_length=160)
    keywords: list[Keyword] = Field(min_length=1, max_length=100)

class CompileCoverLetterRequest(BaseModel):
    company: str; role: str                        # same constraints as CompileRequest
    paragraphs: list[LetterParagraph] = Field(min_length=1, max_length=8)
    keywords: list[Keyword] = Field(default_factory=list, max_length=100)
    confirmed_by_user: bool = False
```

The client passes the analysis it already holds (`state.analysis` in
`popup.js`) into `StartCoverLetterRequest` — do **not** re-run
`extract_keywords` inside the letter job; that would double the ~minute of
model latency for data the extension already has. v1 scope: the letter
button only appears on the results screen after a tailor pass completes.

### 2. `backend/app/llm.py` — `draft_cover_letter(job_text, company, role, keywords, resume_source)`

Same `_call` plumbing as `generate_edits` (flattened wire schema — remember
Ollama 0.32 rejected nested `$ref` schemas, PLAN.md §9.4). Prompt inputs:
the plain-text bullet catalog (`bullet_catalog`), the technologies rows, the
posting keywords, company/role. Prompt rules (state each explicitly, with
the same treat-posting-as-untrusted-data framing the existing prompts use):
3–4 paragraphs; only claim skills/experience from the provided resume
content; never claim a technology from the posting that the resume lacks —
acknowledge the gap or omit it; no dates/degrees/titles beyond what the
resume states; plain text only, no LaTeX. Prompt compliance is UX, not
safety — the server check is the boundary, exactly as §11.1 records for
bullets.

### 3. New file: `backend/app/letter.py` — validation + template

- `validate_letter_paragraph(resume_source, text, keywords, company, role)
  -> list[str]` implementing the safety model above (import the private
  helpers from `resume_parser` as `fit.py` does).
- `render_letter_tex(company, role, paragraphs) -> str`: load
  `cover_letter_template.tex` (new file, repo root, committed), substitute
  escaped values into `<<COMPANY>>`/`<<ROLE>>`/`<<BODY>>` markers via plain
  `str.replace` — markers chosen to be impossible in real LaTeX text.
  Escaping order matters: backslash-escape `\` first (to `\textbackslash{}`),
  then `& % $ # _ { } ~ ^`. The template is a **standalone plain LaTeX
  file** (`\documentclass{article}` + `geometry` matching resume.tex's
  margins + `parskip`; same serif default as the resume) with the
  candidate's name/contact header hardcoded to match `resume.tex`'s header
  block. Do not reuse `resume.cls` — its `\renewcommand{\document}`
  auto-prints the resume header and expects `rSection` structure (PLAN.md
  §8.1); fighting that class is more fragile than 30 lines of article
  preamble.
- Compilation goes through a small generalization of
  `latex_compile.compile_tex`: it currently hardcodes `resume.tex` naming
  and copies `resume.cls` (`latex_compile.py:56-63`). Add an optional
  parameter (e.g. `aux_files: list[Path] | None`, plus writing the given
  source under a caller-chosen name) rather than duplicating the
  subprocess/timeout/page-count logic — the `-no-shell-escape`, two-pass,
  and `pdfinfo` behavior must stay byte-identical for both document types.
  Letters enforce **exactly one page** too (§12.1 applies: a letter that
  spills to page two is a length failure, surfaced with the same
  shorten-and-retry message pattern).

### 4. `backend/app/jobs.py` + `backend/app/main.py` — endpoints

Reuse the async job pattern (MV3 service workers get killed mid-fetch; the
~minute model call must live in the backend — same reasoning as
`CODEX_TASK_async_tailor_job.md`, don't regress to a sync endpoint):

- Generalize `TailorJob` minimally: add `paragraphs:
  list[ReviewedParagraph] = field(default_factory=list)` and a `kind:
  Literal["tailor", "letter"] = "tailor"` field, or add a parallel dataclass
  — implementer's choice, but keep one `_jobs` store and one lock.
- `POST /cover-letter/start` → validates via `require_extension_origin` +
  `_check_job_size`, spawns a thread: `draft_cover_letter(...)`, then
  `validate_letter_paragraph` per paragraph, then
  `update_job(..., status="done", paragraphs=reviewed)`.
- `GET /cover-letter/status/{job_id}` → status/step/paragraphs/error (new
  `CoverLetterStatusResponse`).
- `POST /cover-letter/compile` → re-validate every paragraph of the
  submitted text; enforce the `confirmed_by_user` rule; render, compile,
  one-page check; write `CoverLetter_<Company>_<Role>.pdf` via
  `_safe_filename_part`; return with `Content-Disposition` exactly like
  `/compile` so the extension's download path is reusable. If the tracker
  task has landed, append a tracker event (add an `event: "letter"` shape or
  a `document` field on `compiled` — pick one and note it in the tracker's
  README section).

## Extension changes

- `popup.html`/`popup.css`: on the results screen, a "Draft cover letter"
  button; a letter view with one textarea per paragraph, per-paragraph flag
  text, the confirm checkbox (hidden unless flags exist), and "Compile
  letter" / back buttons.
- `popup.js`: letter state alongside `state.edits`; send
  `START_COVER_LETTER` with `{jobText, company, role, keywords}` (the job
  text must be re-extracted or — better — stored by `startTailor` in
  `chrome.storage.local` at tailor time so the letter reuses the exact same
  posting text; do that, it also survives popup reopen).
- `background.js`: `START_COVER_LETTER` handler mirroring `startTailor`
  (`background.js:35-49`) with its own alarm name + storage keys
  (`letterResult`, `activeLetterJobId`) — don't multiplex into the tailor
  keys, stale-state bugs here were §c809989's whole point. Polling mirrors
  `pollTailorJob` including the outage-recovery branch. Compile+download
  goes through a message that reuses `compileAndDownload`'s response
  handling — refactor it to take the endpoint path + payload rather than
  copy-pasting the blob-URL lifecycle (`background.js:98-172`); that
  lifecycle encodes hard-won Firefox behavior (§11.2) and must have exactly
  one implementation.

## Verification

- Unit tests: paragraph validation — ungrounded number flagged; grounded
  number (present anywhere in resume) passes; posting-keyword tech absent
  from resume flagged (regression-style test naming a technology the resume
  genuinely lacks, mirroring the §10 test approach); company name containing
  the pattern-matched strings never false-flags; every LaTeX-unsafe input
  class hard-rejected; escaping round-trip (`50% & more_`) compiles.
- API tests (mocked LLM): start/status flow; compile with flags +
  `confirmed_by_user: false` → 422; same with `true` → 200; LaTeX-unsafe
  paragraph → 422 even with `true`.
- Template: compile `cover_letter_template.tex` with placeholder body
  through the real pdflatex once; exactly one page. Add it to the CI
  latex-smoke job (`CODEX_TASK_github_actions_ci.md`) alongside
  `resume.tex`.
- Node test: extend `extension/tests/background.test.js` to cover the
  refactored shared download path for the letter endpoint (bytes intact,
  blob URL revoked on terminal state).
- Manual end-to-end: real posting → tailor → draft letter → edit a
  paragraph → compile → PDF opens, one page, named
  `CoverLetter_<Company>_<Role>.pdf`.
