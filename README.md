# Resume Tailor

A local, review-gated Firefox workflow that reads the job posting in your active tab, scores keyword coverage, proposes factual edits to the LaTeX resume, and compiles selected edits or a reviewed cover letter to PDF. Chrome and Edge remain supported. The base [`resume.tex`](resume.tex) is never modified.

## What is implemented

- Firefox/Chrome/Edge Manifest V3 extension with on-demand DOM text extraction.
- FastAPI backend bound to localhost with a shared-secret/origin guard.
- Fully local Ollama inference for posting analysis and addressable resume edits.
- Server-side checks for unknown targets, target-bullet-grounded wording/entities/numbers, immutable skill-list membership, duplicate targets, unsafe LaTeX, and malformed braces.
- Mandatory per-edit review; flagged edits are visible but cannot be selected.
- Deterministic weighted job-fit scoring, with missing keywords shown before review.
- Review-gated cover letters with resume-global grounding checks and explicit confirmation for flagged claims.
- An append-only local application tracker with outcome recording and a self-contained HTML report.
- Isolated two-pass `pdflatex -no-shell-escape` compilation, mandatory one-page verification, and named PDF output.

## Quick start with Docker

For a new machine, Docker Desktop is the only backend prerequisite. From this repository run:

```powershell
docker compose build
docker compose run --rm backend python -m backend.app.configure
docker compose up
```

The configure command creates the ignored `backend/.env` and matching ignored `extension/config.local.js` without printing the shared secret. The first `docker compose up` downloads the configured Ollama model (roughly 5 GB) into a named volume; later starts reuse it. The container includes Python, Ollama, TeX Live, and `pdfinfo`, publishes the backend only on `127.0.0.1:8765`, mounts the personal `resume.tex` at runtime, and persists PDFs and tracker data on the host.

Container inference defaults to CPU because portable Docker Desktop GPU passthrough is not available for this project's AMD setup. Tailoring may therefore be noticeably slower than the native host setup. After the backend reports that it is ready, load the unpacked extension using the browser instructions below. Stop the stack with `docker compose down`; do not add `-v` unless you intentionally want to remove the downloaded Ollama model.

## Native one-time setup

From PowerShell in this repository:

```powershell
python -m pip install -r backend/requirements.txt
```

Run local configuration:

```powershell
.\configure.cmd
```

This creates ignored `backend/.env`, generates a cryptographically random shared secret, and writes the matching secret to ignored `extension/config.local.js`. Secret values are never printed.

Install Ollama for Windows and download the configured local model once:

```powershell
winget install --id Ollama.Ollama -e
ollama pull qwen2.5:7b-instruct
```

Ollama runs locally at `http://127.0.0.1:11434`; no API key, cloud account, or usage credits are required. This machine has Ollama and the model installed already.

On this machine Ollama reports `qwen2.5:7b-instruct` running 100% on the AMD GPU with a 16K context. `ollama ps` shows the active processor and context allocation.

MiKTeX is already installed on this machine. The backend automatically detects its usual per-user install path; `PDFLATEX_PATH` and `PDFINFO_PATH` in `.env` can override the compiler and page-count tool.

Load the extension in Firefox:

1. Open `about:debugging`.
2. Select **This Firefox**.
3. Select **Load Temporary Add-on**.
4. Select [`extension/manifest.json`](extension/manifest.json).

Firefox removes temporary add-ons when it restarts, so repeat these four steps after a browser restart. The generated shared secret is loaded automatically; the popup settings remain available for overrides.

For Chrome or Edge, enable Developer mode on the extensions page, choose **Load unpacked**, and select the [`extension`](extension) folder.

## Run

Start the backend:

```powershell
.\start_backend.cmd
```

Then open a single job posting, click the extension, and choose **Tailor this resume**. Review the fit score and every proposed change, then compile only the checked edits. From the review screen you can also draft a cover letter, edit every paragraph, confirm any grounding warnings, and compile it. PDFs are retained in [`output`](output).

The health endpoint is available at `http://127.0.0.1:8765/health`. The API intentionally refuses tailoring/compile requests until either `SHARED_SECRET` or an exact `ALLOWED_ORIGIN` is configured.

## Verification

Run the automated suite:

```powershell
python -m unittest discover -s backend/tests -v
node extension/tests/background.test.js
```

Run the real Firefox package and authenticated HTTP compile smoke tests:

```powershell
python backend/tests/firefox_smoke.py
python backend/tests/http_smoke.py
python backend/tests/ollama_smoke.py
```

The suite covers parsing, fit scoring, tracking, letter grounding, edit application, target-level traceability and LaTeX defenses, authentication, Firefox CORS, cross-browser API selection, and API compile gating. The Node regression test proves Firefox receives a background-owned Blob URL, preserves every PDF byte, and revokes the URL only after the download reaches a terminal state. The Firefox smoke test installs and removes the extension in an isolated temporary Firefox profile. The HTTP smoke test starts the real backend and compiles into a temporary output directory. The Ollama smoke test exercises the local-model calls, fit report, safety review, resume edits, cover-letter review, and both PDF compilation paths through real HTTP endpoints.

GitHub Actions runs the mocked backend suite, JavaScript syntax/background regressions, and real one-page LaTeX smoke compiles for both document templates on pushes and pull requests to `main`. Ollama and browser smoke tests remain manual.

## Tracking applications

Every successfully compiled resume is appended to the ignored local file `data/applications.jsonl`. Cover-letter output is linked to the matching tracked company and role when possible. Manage the log without exposing a new HTTP endpoint:

```powershell
python -m backend.app.tracker list
python -m backend.app.tracker outcome latest applied --note "Submitted through company portal"
python -m backend.app.tracker report
```

The report is written to `output/applications_report.html`. An unambiguous application ID prefix can replace `latest`, and valid outcomes are `applied`, `screen`, `interview`, `offer`, `rejected`, and `ghosted`.

With Docker, prefix the same commands with `docker compose run --rm backend`, for example:

```powershell
docker compose run --rm backend python -m backend.app.tracker list
```

## Important behavior

- Education, dates, titles, degrees, and the base file are never edited by the pipeline.
- Technology rows can only be reordered; adding or deleting a member is rejected.
- Bullet edits can change wording but cannot introduce factual content, technologies, proper-name entities, or numeric claims absent from that exact original bullet.
- Content copied from a different bullet is rejected, and a PDF is never saved or returned unless it is exactly one page.
- Cover-letter numbers, known entities, and posting keywords are checked against the whole resume; ungrounded claims require explicit human confirmation, while LaTeX-unsafe text is always rejected.
- PDF compilation and saving run in the extension background context, so closing or unfocusing the popup cannot tear down an in-flight download.
- The job posting is treated as untrusted data in both local-model prompts.
- Compiling with no selected edits produces the unchanged base resume.
