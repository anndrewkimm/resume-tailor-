# Task: GitHub Actions CI — run the existing suites and a real LaTeX smoke compile on every push

## Why this task exists (read this first)

The repo has 40 passing Python unit tests plus a Node background-download
regression, but they only run when someone remembers to run them locally.
Every safety property this project cares about — the anti-fabrication
validator, the Firefox blob-URL download lifecycle, technology-membership
immutability — is enforced by those tests, so an unnoticed regression is a
safety regression. CI makes them run on every push.

Key fact that shapes this task: **the unit suite is fully mocked and fast**
(`python -m unittest discover -s backend/tests` = 40 tests in ~0.2s; every
`compile_tex` and Ollama call is patched — verified 2026-07-18). No Ollama,
no model download, and no LaTeX distribution is needed for the unit jobs.
The real-toolchain smoke scripts (`ollama_smoke.py`, `firefox_smoke.py`,
`http_smoke.py`) don't match unittest's `test*.py` discovery pattern and are
**deliberately excluded from CI** — they need a live Ollama/Firefox and stay
manual.

What CI *should* additionally do is the one real-toolchain check that's
cheap and hermetic: compile `resume.tex` with the same flags production uses
and assert exactly one page — the §12.1 hard requirement — so a template or
class edit that breaks compilation or spills to two pages fails the build.

## Scope

One new file: `.github/workflows/ci.yml`. No source, test, or config changes
— **except** if a test turns out to depend on local machine state (see
"clean-checkout rule" below), in which case fix that test's isolation, not
the workflow.

### Clean-checkout rule

CI runs on a checkout with **no `backend/.env` and no
`extension/config.local.js`** (both gitignored). The suites are believed
clean today — `config.py` tolerates a missing `.env` (`load_dotenv` no-ops;
`SHARED_SECRET` defaults to `""` and tests set config values directly per
PLAN.md §9.1), and `popup.js` guards with `globalThis.RESUME_TAILOR_LOCAL ??
{}`. But verify by running both suites in a scratch clone without those
files before writing the workflow; if anything fails, fix the test's
isolation the way `ApiTests` handles `SHARED_SECRET`/`OUTPUT_DIR`.

## The workflow

`name: ci`, triggers: `push` and `pull_request` on `main`. Three jobs, all
`runs-on: ubuntu-latest` (the backend is OS-neutral Python; Windows is the
dev machine, not a CI requirement — don't add a Windows matrix, it doubles
minutes for a localhost-only personal tool).

### Job 1: `backend-tests`

- `actions/checkout@v4`, `actions/setup-python@v5` with `python-version:
  "3.12"`. Rationale: the code needs ≥3.11 (`datetime.UTC`, PEP 604 in
  runtime positions); the dev machine runs 3.14, but `requirements.txt` is
  deliberately unpinned (PLAN.md §8.2) so any modern version resolves. 3.12
  is the safe wheels-everywhere floor; bumping later is a one-line change.
- `pip install -r backend/requirements.txt`
- `python -m unittest discover -s backend/tests -v`

### Job 2: `latex-smoke`

- Install the minimal TeX set, aligned with
  `CODEX_TASK_docker_single_command_setup.md` (keep the two package lists in
  sync — if the Docker task discovered a different working set, use that):
  `sudo apt-get update && sudo apt-get install -y --no-install-recommends
  texlive-latex-base texlive-latex-recommended texlive-fonts-recommended
  poppler-utils`. Add `texlive-latex-extra` only if the compile genuinely
  fails without it — `resume.tex`/`resume.cls` need only `parskip`, `array`,
  `ifthen`, `geometry`, `hyperref` (re-grep at implementation time).
- Compile exactly as production does (`latex_compile.py:66-80`): run
  `pdflatex -no-shell-escape -interaction=nonstopmode -halt-on-error
  resume.tex` **twice**, in a scratch directory containing copies of
  `resume.tex` + `resume.cls` (don't dirty the checkout; `.aux`/`.log` are
  gitignored locally but CI should still compile in a temp dir to mirror
  `compile_tex`'s isolation).
- Assert `resume.pdf` exists, then assert `pdfinfo resume.pdf` reports
  `Pages: 1` — this makes §12.1's one-page invariant a CI-enforced property
  of the committed template, e.g.:
  `pdfinfo resume.pdf | grep -E '^Pages:\s+1$'`.

### Job 3: `extension-tests`

- `actions/setup-node@v4` with a current LTS (`node-version: "22"` — the
  test is plain-Node, no npm install, so version sensitivity is low).
- `node --check` every `.js` file in `extension/` (excluding
  `config.local.js`, absent in CI anyway) — mirrors the "JavaScript syntax
  validation" the plan's verification notes rely on:
  `for f in extension/*.js extension/tests/*.js; do node --check "$f"; done`
- `node extension/tests/background.test.js`

## Deliberate exclusions (document these in a comment at the top of ci.yml)

- No Ollama in CI: model-dependent behavior is untestable deterministically
  and the pull is ~5GB; `ollama_smoke.py` stays a manual pre-release check.
- No Firefox/`firefox_smoke.py`, no `http_smoke.py`: real-browser install
  and live-server smoke tests stay manual for the same reason.
- No caching of the TeX apt packages in v1 — the install is ~1-2 minutes;
  add `actions/cache` later only if CI time actually becomes annoying.
  (pip caching via `setup-python`'s `cache: pip` input is free — do enable
  that.)

## Verification

- Push a branch with the workflow; all three jobs green on GitHub.
- Prove each job can actually fail (temporarily, on the branch, then
  revert): break a Python assertion → job 1 red; add a bogus
  `\usepackage{doesnotexist}` to a scratch commit of `resume.tex` → job 2
  red; introduce a syntax error in `popup.js` → job 3 red. A CI that has
  never been seen red is unverified.
- Confirm the workflow triggers on `pull_request` by opening a draft PR.
