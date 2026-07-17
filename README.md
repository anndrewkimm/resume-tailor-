# Resume Tailor

A local, review-gated Firefox workflow that reads the job posting in your active tab, proposes factual edits to the LaTeX resume, and compiles selected edits to PDF. Chrome and Edge remain supported. The base [`resume.tex`](resume.tex) is never modified.

## What is implemented

- Firefox/Chrome/Edge Manifest V3 extension with on-demand DOM text extraction.
- FastAPI backend bound to localhost with a shared-secret/origin guard.
- Fully local Ollama inference for posting analysis and addressable resume edits.
- Server-side checks for unknown targets, target-bullet-grounded wording/entities/numbers, immutable skill-list membership, duplicate targets, unsafe LaTeX, and malformed braces.
- Mandatory per-edit review; flagged edits are visible but cannot be selected.
- Isolated two-pass `pdflatex -no-shell-escape` compilation, mandatory one-page verification, and named PDF output.

## One-time setup

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

Then open a single job posting, click the extension, and choose **Tailor this resume**. Review every proposed change and compile only the checked edits. PDFs are also retained in [`output`](output).

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

The suite covers parsing, wrapped technology rows, edit application, target-level traceability and LaTeX defenses, authentication, Firefox CORS, cross-browser API selection, and API compile gating. The Node regression test proves Firefox receives a background-owned Blob URL, preserves every PDF byte, and revokes the URL only after the download reaches a terminal state. The Firefox smoke test installs and removes the extension in an isolated temporary Firefox profile. The HTTP smoke test starts the real backend and compiles into a temporary output directory. The Ollama smoke test exercises both local-model calls, safety review, edit application, and PDF compilation through real HTTP endpoints.

## Important behavior

- Education, dates, titles, degrees, and the base file are never edited by the pipeline.
- Technology rows can only be reordered; adding or deleting a member is rejected.
- Bullet edits can change wording but cannot introduce factual content, technologies, proper-name entities, or numeric claims absent from that exact original bullet.
- Content copied from a different bullet is rejected, and a PDF is never saved or returned unless it is exactly one page.
- PDF compilation and saving run in the extension background context, so closing or unfocusing the popup cannot tear down an in-flight download.
- The job posting is treated as untrusted data in both local-model prompts.
- Compiling with no selected edits produces the unchanged base resume.
