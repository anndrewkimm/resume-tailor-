# Task: package the backend + Ollama + TeX Live into one Docker container so a friend needs only Docker Desktop

## Why this task exists (read this first)

Today, getting this running on a new machine (per `README.md`) requires
installing, in order: Python, `pip install -r backend/requirements.txt`,
Ollama for Windows via `winget`, a ~5GB `ollama pull qwen2.5:7b-instruct`
model download, and MiKTeX (a multi-GB LaTeX distribution), plus running
`configure.cmd` to generate a shared secret. That is a lot to ask someone to
install just to try the extension out.

The goal of this task is to collapse **all of that host-side software** —
Python, pip packages, Ollama, and MiKTeX/TeX Live — into a single Docker
image, so a friend's only prerequisites are:

1. Docker Desktop.
2. Loading the unpacked extension in their browser (unavoidable — Manifest
   V3 temporary/unpacked extensions are a browser-side step, not something
   Docker can remove).

Everything else — generating the shared secret, running the backend,
running Ollama, compiling LaTeX — happens inside the container.

## Architecture decision: one container, not docker-compose with two services

Bundle the FastAPI backend, Ollama, and TeX Live into a **single image**
(`ollama serve` and `uvicorn` both run in it, supervised by a small
entrypoint script), rather than splitting Ollama into its own container
next to a backend container.

This is a deliberate choice, not just simplicity for its own sake — read
`backend/app/llm.py:17-21` (`_local_ollama_url`):

```python
def _local_ollama_url() -> str:
    parsed = urlparse(config.OLLAMA_HOST)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise LLMError("OLLAMA_HOST must be a local HTTP address")
    return f"{config.OLLAMA_HOST}/api/chat"
```

This is a deliberate security guard (see `PLAN.md`) that stops the backend
from ever being pointed at an attacker-controlled or remote LLM endpoint —
it only trusts loopback addresses. A two-container compose setup would need
`OLLAMA_HOST=http://ollama:11434` (the Docker service DNS name), which this
check would reject, forcing you to either weaken the allowlist to include
container-network hostnames, or special-case Docker. Keeping Ollama in the
same container means `OLLAMA_HOST` stays `http://127.0.0.1:11434`, exactly
as it is today, and **this file does not need to change at all.**

Do not "fix" `_local_ollama_url` to allow other hostnames as part of this
task — if a future task genuinely needs multi-container, that check should
be revisited deliberately and separately, not as a side effect of a Docker
task.

## Scope

New files only, plus one doc update. Do not change `backend/app/main.py`,
`backend/app/llm.py`, `backend/app/jobs.py`, `resume_parser.py`, the
extension, or any existing test. Nothing about how the API behaves changes;
only how it's launched.

## New files

### 1. `backend/Dockerfile`

Multi-stage not required (this is a dev-machine convenience image, not a
production deploy) — a single `python:3.12-slim` stage is fine, but pin the
minor Python version to whatever `backend/requirements.txt` and the code
actually need (the code uses `datetime.UTC` and `int | None` — needs
Python >= 3.11).

Install, in order:

1. `apt-get install`: `poppler-utils` (provides `pdfinfo`, used by
   `latex_compile.py`), `curl` (to fetch the Ollama install script), and a
   **minimal** TeX Live, not the full distribution (`texlive-full` is
   several GB and mostly unused here). `resume.cls`/`resume.tex` only pull
   in `parskip`, `array`, `ifthen`, `geometry`, `hyperref` (verify this
   against the actual files at implementation time —
   `grep -n usepackage resume.cls resume.tex` — package names can drift).
   Start with `texlive-latex-base texlive-latex-recommended
   texlive-fonts-recommended` and only add `texlive-latex-extra` if a real
   compile fails with a missing-package error. Verify by actually compiling
   `resume.tex` inside the built image before calling this done — don't
   guess the package set from a docs table.
2. Install Ollama using its official Linux install script
   (`curl -fsSL https://ollama.com/install.sh | sh`) rather than trying to
   run the Windows installer logic — this is a Linux container regardless
   of the host OS.
3. `pip install -r backend/requirements.txt`.
4. Copy `backend/app` and `resume.cls` into the image at the same relative
   layout `backend/app/config.py` already expects
   (`REPO_ROOT/resume.tex`, `REPO_ROOT/resume.cls`, `REPO_ROOT/output`) —
   i.e. image layout `/app/resume.cls`, `/app/backend/app/...`,
   `/app/output/` — so the existing default paths in `config.py` resolve
   correctly with zero new env vars.
5. **Do not bake `resume.tex` into the image.** It's the user's personal
   resume, not shared example content, and a friend testing this needs to
   supply their own. Ship a placeholder `resume.tex` (or reuse this repo's
   as an example) but document that a friend should bind-mount their own
   file over `/app/resume.tex`.

### 2. `backend/entrypoint.sh`

Responsible for, in order:

1. Start `ollama serve` in the background (`&`), redirecting its output to
   stdout/stderr so `docker logs` shows both processes' output prefixed
   distinguishably (e.g. prefix Ollama's lines, or run it under a tiny
   process supervisor — a hand-rolled `&` + `wait` is fine here, this
   doesn't need `supervisord`).
2. Poll `http://127.0.0.1:11434` until it responds (a `curl` retry loop
   with a timeout, so we don't race the model pull against a not-yet-ready
   server).
3. Run `ollama pull "$OLLAMA_MODEL"` (default `qwen2.5:7b-instruct`, same
   default as `backend/.env.example`) **only if the model isn't already
   present** (`ollama list | grep -q` first) — this makes container
   restarts fast; the ~5GB pull only happens once, and should land in a
   named volume (see compose file below) so it survives
   `docker compose down`/`up` and image rebuilds.
4. Exec `uvicorn backend.app.main:app --host 0.0.0.0 --port 8765` (note:
   `0.0.0.0` inside the container is correct and still only reachable via
   the published `127.0.0.1:8765` on the host — this is not the same as
   the security guard in `security.py`, which is about origin/secret
   checking, not bind address).
5. Print a clear log line before the pull starts, e.g.
   `"First run: downloading qwen2.5:7b-instruct (~5GB), this can take a
   while..."` — a friend watching `docker compose up` with no explanation
   for a multi-minute pause will assume it's broken.

### 3. `backend/app/configure.py` (new — replaces the host-side configure step)

Port the logic from `configure.ps1`/`configure.cmd` into a small Python
script that:

- Reads `backend/.env.example`, generates a random `SHARED_SECRET` if
  `backend/.env` doesn't already have a real one (reuse the "replace-with-"
  placeholder check from `configure.ps1`).
- Writes `backend/.env`.
- Writes `extension/config.local.js` with the matching secret, in the same
  `globalThis.RESUME_TAILOR_LOCAL = Object.freeze({ sharedSecret: '...' })`
  format `configure.ps1` already produces — check `extension/` for how this
  file is actually read before assuming the format is stable.
- Never prints the secret, matching the existing scripts' behavior.

This is the piece that removes the "friend needs Python" requirement: it
runs **inside the container** via
`docker compose run --rm backend python -m backend.app.configure`, using
the image's own Python, writing to `backend/.env` and
`extension/config.local.js` on the host through bind mounts (see below).
`configure.ps1`/`configure.cmd` stay as-is for Windows host-side use
(don't delete them — some users may still want to run the backend
natively); this is an additional path, not a replacement.

### 4. `docker-compose.yml` (repo root)

One service, `backend`:

```yaml
services:
  backend:
    build:
      context: .
      dockerfile: backend/Dockerfile
    ports:
      - "127.0.0.1:8765:8765"
    volumes:
      - ./backend/.env:/app/backend/.env
      - ./resume.tex:/app/resume.tex
      - ./resume.cls:/app/resume.cls
      - ./output:/app/output
      - ollama-models:/root/.ollama
    environment:
      - OLLAMA_MODEL=qwen2.5:7b-instruct

volumes:
  ollama-models:
```

Notes for whoever implements this:

- Publish only to `127.0.0.1:8765`, not `0.0.0.0:8765` — the whole point of
  `security.py`'s origin/secret check is that this stays a localhost-only
  service; don't accidentally expose it to the LAN via the compose port
  mapping.
- The `ollama-models` named volume is what makes the ~5GB pull a one-time
  cost across `docker compose down`/`up` cycles — an anonymous or missing
  volume would silently re-pull the model on every recreate.
- Bind-mounting `backend/.env` directly (not the whole `backend/` dir)
  means the image's own copy of `backend/app` (baked in at build time) is
  what actually runs; a friend editing `backend/app` on the host won't
  affect a running container without a rebuild. That's intentional here —
  this is meant to be a "just run it" path for a friend, not a live-reload
  dev setup. If iterating on backend code, use `start_backend.ps1`/`.cmd`
  directly instead of Docker.

## Friend-facing setup (add to README.md, don't replace the existing native
instructions — add a new "Quick start with Docker" section above them)

```powershell
docker compose build
docker compose run --rm backend python -m backend.app.configure
docker compose up
```

Then load the extension exactly as the existing README section describes
(that step is unavoidable and unrelated to Docker). First `docker compose
up` will pause for several minutes on the model pull — that's expected and
logged.

## Explicitly out of scope / do not attempt

- **GPU passthrough.** The README notes this machine runs the model 100%
  on an AMD GPU. Docker Desktop GPU passthrough is NVIDIA/WSL2-specific and
  unreliable for AMD; don't try to wire this up. Default to CPU inference
  in the container — call this out in the README as "slower than the host
  setup, especially on a friend's machine without a discrete GPU," so
  nobody is surprised results take longer than the ~1 minute the existing
  docs describe.
- **Rewriting `configure.ps1`/`configure.cmd`.** They keep working for
  native/non-Docker use; don't delete or fold them into `configure.py`.
- **macOS/Linux native (non-Docker) host support.** Out of scope — Docker
  Desktop already covers cross-platform for the containerized path, so
  there's no need to also write a bash equivalent of `configure.ps1`.
- **Changing `_local_ollama_url` in `llm.py`.** Covered above — the
  single-container design exists specifically so this doesn't need to
  change.

## Verification

- `docker compose build` succeeds.
- `docker compose run --rm backend python -m backend.app.configure`
  produces a real `backend/.env` (not the placeholder secret) and a real
  `extension/config.local.js`.
- `docker compose up`, then `curl http://127.0.0.1:8765/health` returns
  `{"status": "ok", ...}` once the model pull finishes.
- With the extension loaded and pointed at a real job posting, a full
  tailor + compile round-trip produces a downloaded PDF, same as the
  native setup.
- `docker compose down && docker compose up` (no `-v`) does **not**
  re-trigger the ~5GB model pull — confirms the named volume is wired
  correctly.
- Existing `python -m unittest discover -s backend/tests -v` still passes
  unmodified (this task must not change backend behavior, only packaging).
