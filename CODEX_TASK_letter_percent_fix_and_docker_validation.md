# Task: fix the cover-letter percent hard-reject bug, then install Docker and validate the container end-to-end

## Context — independent review results (2026-07-18, read this first)

The implementations of all five prior specs (`CODEX_TASK_docker_single_command_setup.md`,
`CODEX_TASK_job_fit_score.md`, `CODEX_TASK_application_tracker.md`,
`CODEX_TASK_github_actions_ci.md`, `CODEX_TASK_cover_letter.md`) were
independently reviewed against their specs. Verdict: **faithful and correct**
— all 58 Python tests and the Node regression pass; the Docker image bakes in
no `resume.tex` and publishes only `127.0.0.1:8765`; the loopback-only
`_local_ollama_url` guard is untouched; fit scoring, tracker, CI jobs, and
the letter grounding split all match their specs. The `/config` mount
approach in `docker-compose.yml` (instead of direct `.env` bind mounts) was
reviewed and accepted as an improvement.

**Exactly one real bug was found**, plus one thing that could not be
verified locally (the Docker image was never built — Docker isn't installed
on this machine). Those two items are this task. **Do not modify anything
else**: no changes to the existing `CODEX_TASK_*.md` files, no edits to
existing PLAN.md sections (append-only, dated notes if needed), no loosening
of any validation outside the precise change below, and no touching
`resume.tex`/`resume.cls`.

## Task 1 — cover letters citing the resume's own percentages error the whole draft

### The bug, precisely

The resume's strongest quantified facts are percentages —
`resume.tex:96` contains `\textbf{90\% accuracy}` and `\textbf{50\%
accuracy}`. A cover letter draft will very plausibly cite them ("…achieving
90% accuracy…"). But `validate_letter_paragraph`
(`backend/app/letter.py:49-52`) treats any raw `%` — and `& _ # $` — as a
**hard failure** (`LetterValidationError`), and `_run_cover_letter_job` in
`backend/app/main.py` calls that validator inline while building the
reviewed paragraphs, so the exception propagates and the **entire letter
job lands in `status: "error"`** instead of flagging one paragraph. Net
effect: the letter feature likely fails the first time the model cites the
resume's best numbers, or the first time it writes "R&D".

This strictness is also unnecessary: `escape_latex`
(`backend/app/letter.py:68-84`) already escapes `% & $ # _ { } ~ ^` and
backslashes safely at render time, so those characters in paragraph text
can never reach pdflatex unescaped. The hard-reject set predates trusting
that escaping path and is stricter than compile safety requires. (The §5.6
threat — model-emitted LaTeX commands — comes only from backslashes.)

### The fix (two coordinated changes)

**(a) Narrow the hard-failure set in `validate_letter_paragraph`:**

- Keep as hard failures: control characters (`ord < 32`) and **any
  backslash** (this is what blocks `\input`, `\write18`, and all command
  smuggling — do not weaken it).
- **Delete** the `[%&_#$]` check and the unbalanced-brace check: braces and
  all five specials are fully neutralized by `escape_latex` at render time,
  and prose legitimately contains `%` and `&`. (The brace-balance check
  matters for bullets because bullet text is spliced into LaTeX with only
  allowlisted escapes — that logic in `resume_parser.validate_edit` is
  **not** part of this task and must not change.)
- Grounding checks (numbers, entities/keywords) stay exactly as they are.

**(b) Make draft-time hard failures per-paragraph flags, not job errors:**

In `_run_cover_letter_job` (`backend/app/main.py`), wrap the per-paragraph
`validate_letter_paragraph` call: on `LetterValidationError`, produce a
`ReviewedParagraph` whose `issues` contains the hard-failure message
(prefix it distinctly, e.g. `"unsafe: contains a forbidden LaTeX
command"`) instead of letting the exception kill the job. The user can then
edit that paragraph in the popup. **Compile-time behavior is unchanged**:
`/cover-letter/compile` already re-validates and returns 422 for hard
failures regardless of `confirmed_by_user` — that boundary stays.

**(c) Prompt alignment:** the bullets prompt already tells the model
"Write normal percent, ampersand, and underscore characters; the
application safely escapes them after generation." Add the equivalent
sentence to the `draft_cover_letter` system prompt in `backend/app/llm.py`
so the model doesn't contort around citing "90%".

### Regression tests (add to `backend/tests/test_letter.py` / `test_api.py`)

1. A paragraph citing a resume-grounded percentage ("…achieving 90%
   accuracy…") produces **no** hard failure and **no** grounding issue, and
   `render_letter_tex` output for it contains `90\%`.
2. "R&D" and an underscore in prose: validated clean, escaped correctly.
3. A paragraph containing a backslash command: the drafting job completes
   with that paragraph flagged (job status `done`, not `error`), and
   `/cover-letter/compile` with that text returns 422 even with
   `confirmed_by_user: true`.
4. An ungrounded number ("…improved throughput 300%…" where 300 is nowhere
   in `resume.tex`) still produces a grounding issue — proving the
   narrowing didn't touch grounding.

## Task 2 — install Docker Desktop and validate the container path for real

The Dockerfile/compose/entrypoint have only been reviewed statically. This
machine (Windows 11 Home, `winget` available) has no Docker. Steps:

1. **Install** (requires an elevated shell; the human user must approve the
   UAC prompt, accept Docker's license on first launch, and reboot if
   Windows asks — pause and hand back to the user at those points rather
   than trying to bypass them):
   ```powershell
   wsl --install --no-distribution   # no-op if WSL2 already present
   winget install -e --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
   ```
   Windows 11 Home only supports the WSL2 backend — leave "Use WSL 2 based
   engine" enabled in Docker Desktop. Expect ~20 GB free disk needed
   (image layers + the ~5 GB model volume).
2. **Validate the compose stack**, in order:
   - `docker compose config` (syntax/interpolation sanity).
   - `docker compose build` — if the TeX package set proves insufficient,
     add the smallest missing package (per the Docker spec's guidance,
     `texlive-latex-extra` only on a proven missing-package error) and note
     what was added and why in the commit message.
   - `docker compose run --rm backend python -m backend.app.configure` —
     confirm it creates/updates host `backend/.env` and
     `extension/config.local.js` (non-placeholder secret; do not print it).
   - `docker compose up` — first run pulls the model (~5 GB; the entrypoint
     logs a warning, this is expected to take a while). Then from the host:
     `curl http://127.0.0.1:8765/health` returns `"status": "ok"`.
   - **In-container compile proof**: with the stack up, run the real HTTP
     compile through the extension or `backend/tests/http_smoke.py`
     pointed at the container, confirming TeX-in-container produces a
     one-page PDF into host `output/`. This is the check that proves the
     minimal TeX package list is actually sufficient — the review could
     not verify it.
   - `docker compose down && docker compose up` — confirm **no re-pull**
     (the `ollama-models` named volume is doing its job; startup log says
     "Using cached Ollama model").
   - `docker compose run --rm backend python -m backend.app.tracker list`
     — confirm the tracker CLI works through the container against host
     `./data`.
3. Do **not** modify `docker-compose.yml`/`Dockerfile`/`entrypoint.sh`
   unless a validation step above actually fails; record any such fix as
   its own commit with the failing symptom in the message.

## When both tasks pass

Commit the working tree in logically separated commits (the five-spec
implementation is currently uncommitted): the feature implementation, the
Task 1 fix, and any Docker-validation fixes — without sweeping in unrelated
files. Push to `origin main` so the new CI workflow gets its first real run,
and confirm all three CI jobs are green on GitHub.

## Verification summary

- `python -m unittest discover -s backend/tests -v` passes (should be >58
  after the new regression tests).
- `node extension/tests/background.test.js` and `node --check` on all
  extension JS pass.
- The Docker checklist in Task 2 completes through the no-re-pull check.
- CI green on GitHub after push.
