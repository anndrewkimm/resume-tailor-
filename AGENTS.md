# Repository guidance

This is a local, review-gated resume and cover-letter workflow: a Manifest V3 browser extension talks only to the authenticated FastAPI backend on loopback, and Ollama remains local. Preserve that security boundary.

## Working rules

- Treat `resume.tex` as factual source material and never modify `resume.tex` or `resume.cls` unless the user explicitly requests a resume/template change.
- Never loosen entity, numeric, copied-bullet, technology-membership, or LaTeX-safety validation as an incidental fix. Bullet edits use target-local grounding; cover letters use resume-global grounding plus explicit human confirmation.
- Long Ollama calls belong in backend-owned threads and are polled through durable extension storage/alarms. Do not place long-running state only in popup or service-worker memory.
- Keep backend and Ollama loopback-only. Do not broaden `_local_ollama_url`, the Docker port binding, CORS, or shared-secret checks without explicit security review.
- Existing uncommitted work may belong to another collaborator. Inspect `git status` and preserve unrelated changes.
- Task specs document design intent, but verify the implementation and tests before assuming a spec has landed.

## Verification

Run after source changes:

```powershell
python -m unittest discover -s backend/tests -v
node --check extension/background.js
node --check extension/content.js
node --check extension/popup.js
node extension/tests/background.test.js
```

When LaTeX is available, also run the real compile checks for both `resume.tex` and `cover_letter_template.tex`. Docker changes require `docker compose config` and `docker compose build` on a machine with Docker Desktop.

Manual pre-release smoke scripts stay outside normal unit discovery: `backend/tests/http_smoke.py`, `backend/tests/ollama_smoke.py`, and `backend/tests/firefox_smoke.py`.
