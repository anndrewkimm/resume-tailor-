# Resume Tailor — Project Plan

## 1. Goal

While browsing a job/internship posting in the browser, click one button to:

1. Read the posting's text off the current page (DOM extraction, not screenshot/OCR).
2. Extract the role's key requirements/keywords.
3. Rephrase/reorder the user's existing LaTeX resume content to surface matching
   keywords — **never inventing skills, tools, or experience that aren't already
   in the resume.**
4. Compile to PDF and hand it back for download, named after the company/role.
5. User reviews the diff before compiling, downloads the PDF, and manually
   fills out/submits the application themselves.

Explicitly out of scope: auto-submitting applications, scraping/batching lists
of job URLs, screenshot/OCR-based page reading. All rejected in earlier
discussion — DOM text extraction and single-job manual triggering are the
agreed scope.

## 2. Non-negotiable safeguard

The base resume (`resume.tex`) is ground truth. The system may only:
- Reorder bullets/skills.
- Rephrase existing bullet text to use the posting's terminology for the same
  underlying fact (e.g. "wrote automated tests" → "built CI/CD test
  pipelines" is fine if that's what the user actually did).
- Reprioritize which existing bullets/skills appear first or are emphasized.

It may never:
- Add a skill, tool, framework, or experience that does not already appear
  somewhere in the base resume.
- Change dates, titles, degrees, or any factual claim.

This is enforced two ways (belt and suspenders — see §5.3):
1. Prompt-level constraint with the full original resume given as the only
   source of truth.
2. Programmatic post-check: every new keyword phrase inserted into the output
   must be traceable to a token/phrase present in the original `resume.tex`.
   If a proposed edit fails this check, it's dropped and flagged to the user
   rather than silently applied.
3. Human review step is mandatory before compiling — no silent auto-apply.

## 3. Architecture

```
[Browser Extension]  --(page text)-->  [Local Backend Service]  --(keywords, diff)-->  [Extension review UI]
        |                                       |
   content script                        LLM call (Claude API)
   reads job posting DOM                  LaTeX edit + compile
        |                                       |
   popup: "Tailor Resume"              pdflatex/xelatex (local)
        |                                       |
   <-- PDF download <---------------------- generated PDF
```

Everything runs locally on the user's machine except the LLM API call itself.
No data leaves the machine except the job posting text + resume text sent to
the LLM provider.

### 3.1 Browser extension (Manifest V3, Chrome/Edge)
- **Content script**: on demand (triggered by popup button, not on every
  page load — avoid unnecessary permissions/perf cost), grabs
  `document.body.innerText` or a smarter readability-style extraction
  (e.g. strip nav/footer, keep main posting body). Fallback to full page text
  if a clean extraction isn't confidently identified.
- **Popup UI**: "Tailor Resume for this posting" button. Shows status
  (extracting → sending → generating → ready), then the diff review screen,
  then a "Compile & Download" button.
- **Storage**: none of substance — no job history needs to persist in the
  extension; the backend can log if desired (§3.4).
- Personal use only — load unpacked, no Chrome Web Store submission needed.

**2026-07-16 correction — actual target browser is Firefox, not Chrome.**
The user's daily browser is Firefox, discovered only after the extension was
already built against the Chrome/Edge assumption above. Firefox does support
Manifest V3 and provides a `chrome.*` compatibility shim, so the existing
`extension/manifest.json` + `popup.js` + `content.js` may work as-is, but
this has **not been verified** — nobody has loaded it into Firefox yet.
Known risk areas to check specifically:
- `chrome.storage.local`, `chrome.tabs.query`, `chrome.scripting.executeScript`,
  and `chrome.downloads.download` (all used in `popup.js`/`content.js`) run
  through Firefox's shim, not native APIs — confirm each behaves the same,
  especially `downloads.download({..., saveAs: true})`.
- Firefox may require a `browser_specific_settings.gecko.id` key in
  `manifest.json` for some install paths — currently absent.
- Loading model differs: Chrome uses "Load unpacked" (select the folder) via
  `chrome://extensions`; Firefox uses `about:debugging` → "This Firefox" →
  "Load Temporary Add-on" (select `manifest.json` directly), and **Firefox
  unloads temporary extensions on browser restart** — there's no persistent
  unpacked-install mode without signing, so this needs to be re-loaded each
  session for now.
Next step: load it in Firefox per the above, click through the popup once,
and fix whatever breaks. If something Chrome-specific needs to change, note
it as a Firefox-specific branch rather than assuming Chrome behavior
generalizes.

### 3.2 Local backend service
- **Decision: Python / FastAPI.** The traceability post-check (§2) is
  essentially light NLP (tokenizing bullets, diffing vocabulary against the
  original resume), which is meaningfully easier in Python than Node. LaTeX
  compilation is just a subprocess call either way, so that wasn't a factor.
- Bound to `127.0.0.1` only, started manually or via a small launcher script.
- **Origin check required**: any webpage's JS can call a `127.0.0.1` port
  regardless of bind address — binding locally is not an access control.
  The server must reject requests unless `Origin` matches the extension's
  `chrome-extension://<id>` (or a shared secret header the extension sends).
  This was missing from the original draft.
- Endpoints:
  - `POST /extract-keywords` — job posting text in → structured list of
    requirements/keywords out (LLM call).
  - `POST /generate-diff` — job posting text + keywords + current
    `resume.tex` in → proposed edits out, as a structured diff (not raw
    rewritten LaTeX — see §5.2 and §5.5), plus the post-check results from §2.
  - `POST /compile` — approved diff + base `resume.tex` in → applies diff to
    a temp copy → runs `pdflatex -no-shell-escape` (see §5.6) twice (for
    refs) → returns PDF bytes or compile error output.
- Reads the user's `resume.tex` from a fixed local path (config file), never
  modifies it in place — always writes to a temp/output copy.

### 3.3 LLM integration
- Use Claude API with structured output (JSON schema / tool use) so the
  response is a typed diff, not freeform text that has to be re-parsed
  loosely.
- System prompt encodes the constraint from §2 explicitly and includes the
  full original resume text as the only allowed source of facts.
- Two-call split (keeps each call focused and auditable):
  1. Keyword/requirement extraction from posting text.
  2. Diff generation: given keywords + resume text, propose section-scoped
     edits (e.g. "skills line", "bullet 3 under Experience X") rather than
     regenerating the whole document — smaller blast radius, easier to
     review and validate.

**2026-07-16 superseding decision — switch to a local model via Ollama,
not the Claude API.** The user does not want to pay for Claude API credits
(separate billing from a Claude.ai chat subscription — this was clarified
and understood). Three alternatives were weighed:
1. Manual copy-paste into Claude.ai chat — zero engineering, zero cost, but
   throws away the entire point of the extension (no automation).
2. Google Gemini free tier — free, keeps a cloud model, but is an external
   account with quotas that can change, and requires the same amount of
   `llm.py` rework as option 3.
3. **Chosen: a local model via [Ollama](https://ollama.com), no network
   call at all.**

Local was chosen over Gemini's free tier for a project-wide reason, not
just cost: §5.4 already establishes "everything local except the LLM
call" as this project's core value — a local model removes that one
exception entirely, permanently, with no external account, key, or billing
relationship to ever manage again. It also fits the existing safety design
better than it might first appear: §5.5's traceability check in
`resume_parser.py` is **model-agnostic** — it catches any fabricated
skill/tool/number regardless of which model proposed the edit, and every
edit still requires human review before compiling (§5.3). A weaker local
model just means more edits get flagged for a closer look, not that
anything fabricated can slip through silently. Given the user's stated
technical comfort level, a one-time local install (same shape as the
MiKTeX install in §8.2) is lower-maintenance long-term than an ongoing
API-key/billing relationship with any provider.

**Concrete migration guidance for `llm.py` (Codex's next task):**
- Replace the `anthropic` SDK calls with plain HTTP requests to Ollama's
  local REST API (default `http://localhost:11434`) — no API key, no
  `anthropic` package dependency needed once this lands (remove it from
  `backend/requirements.txt`, add `httpx` or `requests` if not already
  present transitively via `fastapi`).
- Ollama's `/api/chat` endpoint supports a `format` parameter for
  constrained/structured JSON output (schema-based structured output on
  recent Ollama versions, or `format: "json"` as a looser fallback on
  older ones) — use whichever the installed Ollama version supports, and
  validate the parsed response against the existing `pydantic` models in
  `models.py` regardless (defense in depth — a local model is more likely
  to emit malformed JSON than Claude's tool-use was).
- **Model recommendation: start with `llama3.1:8b` or `qwen2.5:7b-instruct`**
  as the default (`ollama pull <model>`) — both run on modest consumer
  hardware (~5-8GB), follow structured-output instructions reasonably
  well, and are a safe default absent specific knowledge of this machine's
  GPU/RAM. If compiled results are frequently flagged as untraceable or
  the model struggles with the JSON schema, step up to a larger variant
  (e.g. `qwen2.5:14b-instruct`) — this is a one-line config change
  (`OLLAMA_MODEL` in `.env`), not a code change.
- `config.py`: replace `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` with
  `OLLAMA_HOST` (default `http://localhost:11434`) and `OLLAMA_MODEL`
  (default `llama3.1:8b`). Update `backend/.env.example` to match — the
  user no longer needs to create an Anthropic API key at all for this to
  work.
- The §3.2 origin/shared-secret check on the backend stays exactly as-is —
  that's about preventing any webpage's JS from calling your local server,
  which matters regardless of which model answers the request.
- **The user still needs to install Ollama itself** (a downloadable
  installer from ollama.com, similar one-time setup to the MiKTeX install)
  and run `ollama pull <model>` once — this is on the user or Codex to
  walk through, not something achievable by editing files alone.
- System prompts in `llm.py` (§3.3, §5.5) were written and tuned against
  Claude's instruction-following; expect to need real prompt iteration
  against the local model's actual outputs — don't assume the same wording
  produces equally reliable structured output on a smaller open model.

### 3.4 LaTeX compile step
- Requires a local LaTeX distribution (TeX Live/MiKTeX) already installed
  since the user authored the resume in LaTeX — confirm which is on this
  machine before building the compile step.
- Backend applies the approved diff to a copy of `resume.tex`, runs the
  compiler in a temp working directory, captures stdout/stderr.
- On compile failure: return the LaTeX error output to the extension/review
  UI rather than failing silently, and don't delete the last known-good PDF.
- Output naming: `Resume_<Company>_<Role>.pdf`, written to a configurable
  output folder (e.g. `~/resume-tailor-/output/`).

### 3.5 Review/diff UI
- Before compiling, show the user a side-by-side or inline diff: original
  bullet/line vs. proposed line, per change, with a checkbox to accept/reject
  each individually (not just an all-or-nothing approve).
- Any edit that failed the §2 post-check is shown but pre-unchecked and
  visually flagged as "not traceable to your resume — review carefully."

## 4. Data flow, concretely

1. User is on a job posting page, clicks extension icon → "Tailor Resume."
2. Content script extracts posting text → sent to
   `POST /extract-keywords`.
3. Backend calls LLM → returns keyword/requirement list → shown briefly or
   passed straight through to step 4.
4. Backend calls `POST /generate-diff` with keywords + `resume.tex` → LLM
   returns structured, section-scoped edits → backend runs the §2
   traceability check on each edit → returns diff + flags to extension.
5. User reviews diff in popup, checks/unchecks individual edits, clicks
   "Compile & Download."
6. Backend applies accepted edits to a temp copy of `resume.tex`, compiles,
   returns PDF.
7. Extension triggers browser download of the PDF, named per §3.4.
8. User manually attaches the PDF to the application and submits it
   themselves.

## 5. Key design decisions and why

### 5.1 DOM text extraction over OCR/screenshots
Job postings are text on a webpage; reading the DOM directly is more
reliable and far simpler than screenshot + OCR. Established earlier in this
conversation — no reason to revisit unless a specific site defeats DOM
extraction (see §6).

### 5.2 Structured, section-scoped diffs over full-document regeneration
Asking the LLM to output a whole new `.tex` file risks larger, harder-to-review
changes and more surface area for LaTeX syntax breakage. Scoping edits to
specific bullets/lines keeps changes small, reviewable, and easy to validate
against §2.

### 5.3 Human-in-the-loop is mandatory, not optional
This was the user's explicit, repeated requirement: the tool must never
silently change the resume to fabricate keyword matches. The review screen
is a hard gate, not a configurable setting.

### 5.4 Everything local except the LLM call
Keeps the resume and job posting data on the user's machine; the backend is
localhost-only with no external exposure.

### 5.5 Traceability check: entity-level, not phrase-level
The original draft had a contradiction: it allowed rephrasing
("wrote automated tests" → "built CI/CD test pipelines") while also
requiring every inserted phrase to be traceable to the original text —
literally impossible, since rephrasing by definition introduces new surface
wording. Resolved as follows:

- The post-check extracts **entities**, not phrases, from each proposed
  bullet: skill/tool/framework/platform names, product names, numbers
  (percentages, counts), and proper nouns. A practical source for the
  candidate list is the Technologies section itself plus capitalized/`\textbf`
  spans already in the resume — anything the LLM bolds or that matches a
  known tech term is treated as a factual claim.
- Any entity in the proposed bullet that doesn't fuzzy-match (case/punct
  -insensitive, simple singular/plural and common-abbreviation aware, e.g.
  "JS" ~ "JavaScript") an entity somewhere in the original `resume.tex` fails
  the check and the edit is flagged, not silently dropped or applied (§3.5).
- Everything else — connective language, verb choice, sentence structure,
  emphasis/ordering — is unrestricted, since it doesn't assert a new fact.
- This means the check is deliberately conservative in one direction (it may
  occasionally flag a legitimate synonym it doesn't recognize) and permissive
  in the other (free rephrasing). Given §5.3's mandatory human review, a
  false flag just means one more line the user glances at — an acceptable
  tradeoff versus false negatives (fabricated skills slipping through).

### 5.6 LaTeX compile safety
Compiling LLM-edited source is code execution risk, not just a formatting
concern: `\input`, `\write18` (shell-escape), and similar commands can read
arbitrary files or run shell commands if they end up in the compiled `.tex`.
Mitigations:
- Always compile with `-no-shell-escape`.
- The diff schema (§5.7) only ever allows substituting **plain text content**
  inside an already-existing `\textmd{...}` / `\item` span — the LLM is never
  given a way to emit new LaTeX commands, macros, or document structure.
  Any proposed edit containing a backslash command outside the small
  allowlist (`\textbf`, `\%`, `\&`, `\_`) is rejected before compilation is
  even attempted.

### 5.7 Concrete section-scoping / addressing scheme
Now that the real `resume.tex` is in the repo, the addressing scheme can be
defined concretely instead of left abstract:

- **No custom ID macros needed.** The resume's own structure already gives
  stable, unique anchors — company names in `\begin{rSubsection}{Company}`
  and project names in `\begin{rSubsection}{Project}` are unique per resume,
  so an edit address is `(section, subsection_anchor_text, item_index)`,
  e.g. `("Experience", "Flexera", 2)` for the third bullet (0-indexed) under
  Flexera. This is resolved by regex/parse against the live `resume.tex` on
  every request — it's not a persisted ID, so it never goes stale.
- **Technologies section is a different shape** — three label:value lines
  inside a `tabular`, not `rSubsection`/`\item`. Addressed instead as
  `("Technologies", "Languages"|"Frameworks & Libraries"|"Tools & Platforms")`,
  with the edit being a reordering of the comma-separated list (reordering
  only — the traceability check in §5.5 makes adding new entries here
  equivalent to fabricating a skill, so this list's *membership* is
  immutable; only its order may change to surface relevant skills first).
  Note the "Frameworks & Libraries" value currently spans two source lines
  (wrapped for the PDF's line width) — the parser must treat it as one
  logical comma-list before regenerating it as (possibly re-wrapped) LaTeX.
- **Education section is left untouched** by the tailoring pipeline entirely
  — GPA, dates, and degree are exactly the kind of fields §2 says must never
  be touched, and "Relevant Coursework" is a single fixed list with no
  per-item bullets to reorder meaningfully in this template.

## 6. Known edge cases / open risks

- **Site variability**: LinkedIn, Handshake, Greenhouse, Lever, Workday all
  structure posting pages differently. A generic "grab visible text, strip
  obvious chrome (nav/footer/ads)" heuristic should cover most cases; some
  sites may need per-site content-script selectors added incrementally as
  they're encountered, rather than solved upfront for every ATS.
- **JS-rendered content**: since the content script runs after the page has
  loaded in the user's real browser (not a headless fetch), this is largely
  a non-issue — the DOM is already fully rendered by the time the user clicks
  the button.
- **LaTeX compile fragility**: a bad edit (unescaped special character,
  broken brace) can break the build. Backend should validate the edited
  `.tex` compiles before declaring success, and surface raw compiler errors
  rather than a generic failure.
- **LLM cost/latency**: two API calls per tailoring pass; for personal use at
  a few/day this is negligible, but worth logging token usage if it becomes
  a concern.
- **Resume structure assumptions**: the diff/edit approach assumes
  `resume.tex` has identifiable, addressable sections (e.g. a skills list,
  discrete `\resumeItem{...}` style bullets). Needs the actual `resume.tex`
  reviewed early to design the section-scoping scheme concretely — this is
  the first thing to nail down before writing extraction/diff code.

## 7. Suggested build order

1. Get `resume.tex` into the repo (or a path config pointing to it) and
   design the section-scoping scheme concretely against its real structure.
2. Backend: `/compile` endpoint first, using the unmodified resume, to prove
   out the local LaTeX toolchain end-to-end before any LLM involvement.
3. Backend: `/extract-keywords` and `/generate-diff`, with the §2
   traceability post-check, tested against a couple of real job postings
   pasted in manually (no extension yet).
4. Extension: content script + popup wired to the already-working backend.
5. Review/diff UI polish (accept/reject per-edit, flagged items).

## 8. Current repo state and environment notes (for whoever implements this)

Steps 1 and part of step 2 from §7 have already been done and verified on
this machine; the notes below capture what exists, what's still missing, and
two non-obvious bugs already hit and fixed so they don't get re-debugged.

### 8.1 What's already in the repo
- `resume.tex` — the user's real Overleaf source, pasted in directly.
- `resume.cls` — **reconstructed**, not the user's original file (they
  couldn't locate it in Overleaf). It's a from-scratch implementation of the
  standard "Medium Length Professional CV" template's class file, built to
  support exactly the commands `resume.tex` uses (`\name`, `\address`,
  `rSection`, `rSubsection`). It has been test-compiled successfully — see
  §8.3. If the user later finds their actual original `resume.cls`, that
  should replace this one (drop-in, same filename).
- `backend/requirements.txt`, `backend/app/{__init__.py, config.py,
  security.py, latex_compile.py, main.py}` — a partial FastAPI scaffold
  covering config loading, the origin/secret check from §3.2, the LaTeX
  compile subprocess wrapper from §5.6, and a working `/compile` endpoint
  that compiles the unmodified resume (build order step 2, first half).
  `/extract-keywords` and `/generate-diff` (step 3) are **not yet
  implemented** — that's the next real work.
- `output/` — empty dir, target for compiled PDFs per §3.4.

### 8.2 Environment gotchas already hit
- **This machine had no LaTeX distribution at all.** MiKTeX was installed
  via `winget install --id MiKTeX.MiKTeX -e --silent
  --accept-source-agreements --accept-package-agreements` (silent flags are
  required — an interactive winget run hangs waiting for a prompt that never
  arrives in a non-interactive shell). After install, `pdflatex.exe` is at
  `C:\Users\<user>\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe`
  but is **not on PATH** in an already-open shell — either use the full path
  or open a fresh shell. MiKTeX also auto-installs missing LaTeX packages on
  first use (via its "on-the-fly" package installer), which makes the first
  compile noticeably slower than subsequent ones — don't mistake this for a
  hang.
- **Python 3.14 on this machine breaks naive `pip install` of
  fastapi/pydantic if versions are pinned to anything not very recent** —
  `pydantic-core` has no prebuilt wheel for cp314 below a certain version,
  so pip falls back to compiling the Rust source, which then fails with a
  missing `link.exe` (no MSVC Build Tools installed). Fix used:
  `requirements.txt` intentionally left **unpinned** so pip resolves to the
  latest `pydantic`/`pydantic-core`/`fastapi`, which do ship cp314 wheels.
  Don't re-pin these to older versions without checking wheel availability
  for whatever Python version is actually installed.

### 8.3 LaTeX bug already found and fixed
The original `resume.tex` never calls `\maketitle` — the name/contact header
is presumably rendered automatically by the user's real (missing)
`resume.cls`. Two approaches were tried for the reconstructed class:
1. Auto-invoke via `\AtBeginDocument{\maketitle}` in the class — **broke the
   build** (`Undefined control sequence \hyper@linkurl` right at
   `\begin{document}`), because this hook fires before `hyperref` (loaded in
   `resume.tex`'s own preamble, after `\documentclass{resume}`) finishes its
   own `\AtBeginDocument` setup, leaving `\href` half-initialized. A
   double-nested `\AtBeginDocument{\AtBeginDocument{\maketitle}}` trick was
   also tried and did not reliably fix the ordering either.
2. **What actually works and is now in place**: `resume.cls` just defines
   `\maketitle` normally (no auto-hook), and `resume.tex` calls `\maketitle`
   explicitly on its own line right after `\begin{document}`. This is the
   standard, unsurprising way LaTeX resume templates do this and avoids the
   hook-ordering fragility entirely. If the user's real original
   `resume.cls` is ever substituted in and turns out to auto-invoke the
   header itself, the explicit `\maketitle` call in `resume.tex` would need
   to be removed to avoid a doubled header — check the compiled PDF for a
   duplicate name/contact block if that swap happens.

### 8.4 Verification status — CONFIRMED
Re-compiled after the §8.3 fix and rendered to PNG: the name/contact header
now appears exactly once, with Education, Technologies, Experience, and
Projects all matching the user's reference PDF. No `hyperref` errors in the
log. `resume.cls` and the explicit `\maketitle` line in `resume.tex` are
correct as committed — no further LaTeX-toolchain work needed here.

## 9. Codex implementation review (2026-07-16) — outstanding fixes

Codex implemented the full stack from §7 steps 2–5: the FastAPI backend
(`backend/app/{models,resume_parser,llm,security,main}.py`), the LLM
integration for `/extract-keywords` and `/generate-diff` with the §5.5
traceability check, and the browser extension (`extension/`). This was
independently reviewed and verified end-to-end on this machine: all 12 unit
tests pass, and a real (non-mocked) `compile_tex()` call against the live
`resume.tex` was run and visually confirmed correct (see §8.4). The
implementation is sound overall — notably it adds a prompt-injection defense
in `llm.py` (explicit instruction not to obey text embedded in the job
posting) that wasn't in this plan, and `resume_parser.py`'s addressing/
validation logic matches §5.7/§5.5 precisely, including the wrapped
Technologies-row handling and skill-membership immutability.

Two concrete bugs found during review, left for the next implementation
pass (not fixed by this review — this plan only documents them):

1. **Test suite pollutes the real `output/` directory.**
   `backend/tests/test_api.py::test_compile_applies_safe_approved_edit`
   patches `backend.app.main.compile_tex` to return a fake `%PDF-test` blob,
   but the `/compile` endpoint still writes that fake PDF to whatever
   `config.OUTPUT_DIR` currently points to — which defaults to the real
   `output/` directory. Running the test suite silently overwrites the
   user's actual compiled resume there. **Fix**: in `ApiTests.setUp`,
   alongside the existing `config.SHARED_SECRET` override, also set
   `config.OUTPUT_DIR` to a `tempfile.TemporaryDirectory()` and restore it
   in `tearDown` (same pattern already used for `SHARED_SECRET`).

2. **Stale default model ID.** `backend/app/config.py`
   (`ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")`)
   and `backend/.env.example` (`ANTHROPIC_MODEL=claude-sonnet-4-5`) both
   default to `claude-sonnet-4-5`. That model ID is still valid, but the
   current model for this tier is `claude-sonnet-5` — update both
   occurrences unless there's a specific reason to pin the older model.

### 9.1 Resolution (2026-07-16)

Both review findings are fixed and verified:

- `ApiTests` now replaces `config.OUTPUT_DIR` with a managed temporary
  directory for every test and restores it during teardown. The stale fake
  PDF created by the earlier test run was removed.
- The default model and environment example now use `claude-sonnet-5`.
- The Sonnet 5 migration review found and fixed one related compatibility
  issue: `temperature=0` was removed because Sonnet 5 rejects non-default
  sampling parameters. These structured extraction calls explicitly disable
  adaptive thinking so their output budget is reserved for tool results.
- A regression test now asserts that the client sends no `temperature`,
  `top_p`, or `top_k` parameter and uses the intended thinking configuration.

All 13 tests pass, JavaScript syntax validation passes, and a test run leaves
the real `output/` directory untouched.

### 9.2 Firefox correction and local configuration (2026-07-16)

The Firefox correction from section 3.1 is implemented:

- The extension prefers Firefox's native Promise-based `browser.*` APIs and
  falls back to `chrome.*` for Chrome/Edge.
- The manifest includes a stable Gecko extension ID and Firefox minimum
  version. The backend CORS policy now permits both `moz-extension://` and
  `chrome-extension://` origins while endpoint authentication still requires
  the shared secret.
- `configure.cmd` generates a cryptographically random shared secret and
  writes matching ignored backend/extension configuration files without
  printing secrets. It also migrates an API key out of `.env.example` and
  restores the safe placeholder.
- `config.py` now loads `backend/.env` by explicit path. The prior implicit
  `load_dotenv()` call did not load that file when the launcher ran from the
  repository root.
- Firefox 152 accepted, installed, and uninstalled the actual extension in an
  isolated temporary profile through Firefox's native WebDriver BiDi API.
- An authenticated real HTTP backend request compiled a valid PDF with
  MiKTeX into an isolated temporary output directory.

### 9.3 Superseded by the §3.3 Ollama decision (2026-07-16)

Everything in §9.1/§9.2 was correct and verified working *at the time* —
but §3.3 now records a decision made afterward: drop the Claude API
entirely and move to a local model via Ollama, because the user does not
want to pay for API credits and a local model fits this project's
"everything local" value better than any paid or free-tier cloud
alternative (full reasoning in §3.3). This means:

- The `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` handling in `config.py`,
  `configure.cmd`/`configure.ps1`'s API-key migration step, and
  `backend/.env.example`'s Anthropic placeholder are all about to become
  dead code — don't keep polishing the Anthropic integration further;
  redirect effort to the §3.3 migration guidance instead.
- **Security note, not a code issue:** `backend/.env` on this machine
  currently contains a real Anthropic API key, which was read and
  displayed in this review session's tool output. The user has been told
  to revoke that key at console.anthropic.com regardless of the Ollama
  migration, since a key that's appeared in a log should be treated as
  compromised. Once the Ollama migration lands, `ANTHROPIC_API_KEY` should
  be removed from `.env` entirely rather than left unused.
- `SHARED_SECRET`, the origin/CORS allowlist work, and the Firefox
  `browser.*`/`chrome.*` compatibility work in §9.2 are all still correct
  and unaffected — none of that was about which LLM answers the request.

The automated suite now contains 19 passing tests, including Firefox CORS,
manifest, API namespace, and local configuration regression coverage. A live
Claude request still requires a real `ANTHROPIC_API_KEY` in `backend/.env`;
the workspace currently contains no API-key-shaped credential.

### 9.4 Ollama migration completed (2026-07-16)

The superseding local-model decision in section 3.3 is fully implemented and
verified:

- Ollama 0.32.1 and `qwen2.5:7b-instruct` are installed. The model occupies
  approximately 4.7 GB on disk and runs with a 16K context. Ollama reports
  100% AMD GPU execution on this machine.
- The Anthropic SDK, API-key configuration, model setting, and runtime code
  were removed. `backend/.env` now contains only local Ollama settings and
  the extension shared secret; no API-key-shaped credential remains in the
  workspace.
- `llm.py` calls the loopback-only Ollama `/api/chat` endpoint with JSON
  schemas and validates every response through the existing Pydantic models.
  The configured host is rejected unless it is local HTTP.
- Ollama 0.32 rejected Pydantic's nested `$ref` plus nullable constrained
  integer grammar for edit targets. The wire schema was flattened while full
  Pydantic validation was retained after generation.
- Technology reordering is now deterministic Python logic rather than model
  output, guaranteeing immutable membership and reducing model latency.
- Ollama receives a clean, address-labeled plain-text catalog instead of raw
  LaTeX. Model bullet text is treated as plain text, safely escaped by Python,
  and rejected if it contains commands, braces, controls, or unsafe specials.
- Validation was tightened to reject all control characters and unescaped
  LaTeX special characters before compile.
- The full local HTTP workflow passed: 10 extracted keywords, 6 reviewed and
  accepted edits, and all 6 edits compiled into a valid 57,178-byte PDF in an
  isolated output directory.

The automated suite now contains 27 passing tests. Firefox installation,
JavaScript syntax, schema validation, local inference, safety review, edit
application, and PDF compilation have all been exercised without a cloud LLM.

## 10. URGENT — §2 safety guarantee is broken in production (2026-07-16)

**Top priority, above everything else in this file.** The user ran the real
extension against a real job posting today and the compiled PDF
(`output/Resume_Company_Role.pdf`) contains fabricated experience that does
not exist anywhere in `resume.tex` — sentences like "Contributed to the
orchestration layer by designing workflows that route and sequence
multi-agent tasks," "Extended agent harnesses by building tooling for
agents to test and validate their outputs," and "Contributed to the Agent
Manager system by implementing guardrails and rollback paths" were inserted
into the Flexera bullets. None of that is real. This is exactly the
fabrication §2 exists to prevent, and it was **not flagged** by the
traceability check — it compiled and would have been downloaded and sent to
a real employer if the user hadn't caught it by eye.

**Root cause:** the §9.3 Ollama migration changed `llm.py` to emit
plain-text-only edits (no `\textbf{}` markup at all, to make output more
reliable from the smaller local model — see §3.3's migration notes). But
`resume_parser.py`'s `validate_edit()` finds "new claims to verify" almost
entirely by scanning for `\textbf{...}` spans in the proposed text (plus a
literal-number regex and a check against the job posting's own extracted
keyword list). With no bold markup ever present anymore, that primary
detection path finds **zero candidates** in every plain-text edit — the
check isn't failing loudly, it's silently finding nothing to check, and a
fabricated sentence with no numbers in it and no exact keyword-list phrase
match sails through as "traceable" with an empty issues list. The plain-text
migration and the entity-detection logic were changed independently and
nobody re-verified they still fit together — a `\textbf{}`-based detector
cannot work against text that no longer contains `\textbf{}`.

**This needs a real fix, not a patch on top of the current approach.** The
detector has to find candidate "new claims" some other way now that there's
no bold markup to anchor on. Directions worth considering for whoever picks
this up (not a decision to make casually — pick one deliberately and justify
it in this file when done):
- Extract noun phrases / capitalized multi-word terms from the *proposed*
  bullet text itself (not just from the keyword list) and require each to
  fuzzy-match something in the original resume — closer to what §5.5
  originally specified before the plain-text migration narrowed it down to
  three specific candidate sources.
- Or: diff the proposed bullet against the *original* bullet at that same
  address (the parser already knows the original text via `locate_target`)
  and flag any newly-introduced multi-word phrase that wasn't in the
  original, rather than trying to classify "is this a factual claim" at all
  — rephrasing existing words is allowed no matter what; genuinely *new*
  multi-word phrases are exactly what's suspicious.
- Whatever approach is chosen, add a regression test using this exact
  fabrication (an "Agent Manager system with guardrails and rollback paths"
  sentence inserted into a bullet that never mentioned it) to prove the fix
  actually catches this specific failure mode, since this is the concrete
  case that slipped through.

**Until this is fixed, treat every compiled PDF as unverified** — the user
must manually read every accepted edit against the original resume before
using it, not just check the "flagged" UI state, since the flagging itself
is currently unreliable for plain-text edits.

## 11. Second bug — downloaded PDF is corrupted (2026-07-16)

Reproduced on the same live run as §10: the user clicked "Compile selected
edits," the native Save File dialog appeared, they saved it, and the saved
file failed to open (Firefox/PDF viewer reported it as a failed/corrupted
download) — even though the backend itself produced a valid PDF (confirmed
by rendering the server-side copy in `output/` directly, which opened and
rendered correctly).

**Root cause**: `extension/manifest.json` declares no `background` script —
only a popup (`popup.html`/`popup.js`). `compile()` in `popup.js` (around
line 96-116) builds the PDF blob and calls
`ext.downloads.download({ url: objectUrl, filename, saveAs: true })`
**directly inside the popup's own script**. When `saveAs: true` opens the
native OS save dialog, that dialog takes OS-level window focus away from
the extension popup. Firefox's (and Chrome's) default behavior is to close
an extension popup the moment it loses focus, which tears down its
JavaScript execution context — including the in-flight download and the
`blob:`/`URL.createObjectURL` object backing it — before the file finishes
writing. The result is a file that gets created (so the user sees a "save"
happen) but never receives its full, correct bytes: a truncated/corrupted
PDF, exactly matching the reported symptom.

**Fix**: move the actual download call out of the popup and into a
persistent background script, which is not torn down when the popup closes.
Concretely:
- Add a `background` entry to `manifest.json` (an MV3 event page /
  background script — confirm the correct Firefox MV3 background shape,
  since Firefox's MV3 background-script support has some differences from
  Chrome's service-worker model here).
- `popup.js`'s `compile()` should send the PDF bytes (or a message telling
  the background script to fetch `/compile` itself and handle the result)
  to the background script via `runtime.sendMessage` / a port, rather than
  calling `downloads.download()` itself.
- The background script performs the actual `downloads.download()` call
  and owns the `blob:`/object-URL lifecycle, since it persists regardless
  of whether the popup is open, closed, or loses focus.
- Add a manual regression check to whatever test coverage exists for this:
  after compiling, close/reopen the popup or otherwise unfocus it during
  the save dialog, and confirm the resulting file still opens correctly —
  the current automated tests don't catch this because they don't exercise
  real focus-loss timing in an actual browser.

This and §10 are both real, user-blocking bugs found during the first live
end-to-end run of the finished extension — prioritize both before any
further feature work.

### 11.1 First fix attempt failed — `data:` URLs are rejected by Firefox (2026-07-16)

A fix landed and was tested live. The download now completes, but opening
the saved file throws immediately in the extension:

```
Type error for parameter options (Error processing url: Error: Access
denied for URL data:application/pdf;base64,JVBERi0xLjUK...)
```

**Diagnosis**: the fix apparently switched the popup from a `blob:` object
URL to embedding the whole PDF as a base64 `data:` URI and passing that
directly to `downloads.download()`. This avoids the popup-teardown problem
from §11's original root cause, but trades it for a **hard Firefox
restriction**: Firefox's `downloads.download()` API refuses `data:` URLs
outright for security reasons — this is documented, intentional Firefox
behavior, not a bug in this codebase, and it's a real Chrome/Firefox
divergence (Chrome allows `data:` URLs here; Firefox does not). Any fix
built or tested only against Chrome semantics would not have caught this.

**Fix**: don't use a `data:` URL at all. Go back to a `blob:` object URL
(which Firefox's downloads API does accept) but — per §11's original
fix — construct and hold that blob in the **background script**, not the
popup, so it isn't the popup's lifetime that determines whether the
download survives. Concretely: popup sends the raw PDF bytes (or the
`/compile` response) to the background script via `runtime.sendMessage`;
the background script does `URL.createObjectURL(blob)` and
`downloads.download({url: blobUrl, filename, saveAs: true})` itself, and
revokes the object URL only after the download completes (listen for the
`downloads.onChanged` event reaching a terminal state, rather than a
timer). Verify this actually survives the popup closing/losing focus
during the save dialog before calling it fixed — that was the original
failure mode this whole chain of fixes is trying to solve.

### 11.1 Safety and download resolution (2026-07-16)

Both production blockers are fixed and regression-tested.

- `validate_edit()` now compares every bullet proposal with the exact
  original target bullet. New numeric claims are target-local, known resume
  entities cannot be moved between bullets, and newly introduced content
  tokens are rejected except for a deliberately small connective/editorial
  allowlist. This choice is conservative by design: uncertain model
  rephrasing is flagged for review instead of being allowed to compile.
- The exact reported "Agent Manager system ... guardrails and rollback
  paths" fabrication is covered by a regression test. Cross-bullet entity
  transfer and borrowing a number that exists elsewhere in the resume are
  also covered. All three fabricated sentences reported in section 10 were
  directly checked and are rejected.
- The Ollama prompt now explicitly forbids moving facts between bullets or
  appending posting requirements. The server remains the enforcement
  boundary; prompt compliance is never trusted as the safety mechanism.
- Deterministic technology matching now considers all extracted posting
  phrases, not only phrases classified as `technology`. This preserves a
  useful, membership-safe path when the local model's bullet proposals are
  conservatively rejected.
- PDF compilation, response-byte ownership, and `downloads.download()` now
  live in `extension/background.js`. The popup sends one
  `COMPILE_AND_DOWNLOAD` message and owns no Blob or object URL. The
  background converts the complete response to a PDF data URL, so popup
  focus loss cannot invalidate the bytes during Firefox's native Save dialog.
- The MV3 manifest declares both `background.scripts` (Firefox event page)
  and `background.service_worker` (Chrome/Edge), the current cross-browser
  shape documented by Mozilla. Firefox 152 accepted, installed, and
  uninstalled the resulting extension in an isolated profile.
- A Node background harness performs a mocked compile response, decodes the
  exact URL passed to the downloads API, and verifies every PDF byte survives.
  Popup/static regressions ensure download ownership cannot drift back into
  `popup.js`.

Verification: 32 automated Python tests pass; all extension JavaScript passes
syntax checks; the background binary regression passes; Firefox temporary
installation passes; and the real local Ollama workflow produced 10 keywords,
9 reviewed edits, 8 safety-approved compiled edits, and a valid 56,120-byte
PDF. The earlier unsafe server-side PDF remains historical output and must not
be used; newly compiled files pass the corrected enforcement path.

### 11.2 Firefox data-URL failure resolved (2026-07-16)

Section 11.1's first download implementation and its earlier resolution note
were incorrect about Firefox: the background context fixed popup teardown, but
passing a `data:application/pdf;base64,...` URL to Firefox's downloads API
still failed immediately with `Access denied for URL`. That design has now
been replaced.

- Firefox's background event page constructs a typed PDF `Blob`, creates the
  object URL there, and passes that `blob:` URL to `downloads.download()`.
- The object URL is tracked by download ID. It is retained through
  in-progress changes and revoked only when `downloads.onChanged` reports
  `complete` or `interrupted`. A synchronous/API rejection also revokes it.
- The popup still owns neither the PDF response nor its object URL, so losing
  popup focus during the native Save dialog cannot tear down the backing data.
- Chrome/Edge MV3 service workers do not expose `URL.createObjectURL`; the
  capability check keeps the base64 data-URL fallback only in that environment,
  where the downloads API supports it. Firefox always takes the Blob branch.
- The regression harness now supplies Firefox's object-URL capability and
  fails if the downloads API receives a `data:` URL. It verifies the Blob's
  bytes and terminal-state cleanup. Static tests also require background Blob
  ownership and the `downloads.onChanged` listener.

The extension JavaScript and focused regressions pass, and Firefox 152 accepts
and installs the corrected package. Mozilla's downloads API guidance explicitly
recommends `URL.createObjectURL()` for generated data and revocation after the
download completes via `downloads.onChanged`; this implementation follows that
lifecycle rather than a timer.

## 12. §10 confirmed still broken, plus a second symptom of the same root cause, plus a new hard requirement (2026-07-16)

Reviewed the actual downloaded PDF the user received after the §11.1 fix.
The download itself now works correctly, but the content problem from §10
is confirmed **still present and unfixed** — the same fabricated sentences
("orchestration layer... multi-agent tasks", "Agent Manager system...
guardrails and rollback paths", "agent harnesses") are still there. Do not
treat §11's fix as license to consider this feature safe to use — §10 is
the blocking issue, not §11.

**A second, more precisely diagnosable symptom of the same root cause was
found in this same PDF.** Under "Game Outcome Prediction Platform," the
first bullet now ends with a sentence that is a **verbatim duplicate** of
the entire second bullet:

- Bullet 0 (edited): "...50% accuracy for 8-class placement prediction.
  **Designed a data pipeline using the Riot Games API to collect, clean,
  and aggregate large-scale match statistics for model training.**"
- Bullet 1 (untouched): "**Designed a data pipeline using the Riot Games
  API to collect, clean, and aggregate large-scale match statistics for
  model training.**"

This is not fabrication — it's real content from elsewhere in the resume —
but it demonstrates the underlying behavioral bug precisely: the model is
not confining `new_text` to a rephrasing of *that bullet's own original
content*. It's **appending an entire extra sentence pulled from somewhere
else** (fabricated in §10's case, copy-pasted from a sibling bullet in this
case). Both symptoms — fabricated additions and duplicated content — are
the same failure mode and directly explain why output is running long
enough to overflow onto a second page (§10/§11 testing both produced
2-page output from resumes that compile to exactly 1 page unedited).

**Fix directions for whoever picks this up**, in addition to §10's original
detection-side fix:
- Consider constraining the prompt/schema more tightly: `new_text` should
  arguably be validated as a rephrasing of *only* the original bullet at
  that exact `(section, anchor, item_index)` — i.e. compare word/phrase
  overlap between `new_text` and *that specific bullet's own original
  text* (via `locate_target`), and flag/reject if `new_text` introduces a
  large contiguous chunk that doesn't derive from that bullet but matches
  near-verbatim text found in a *different* bullet or elsewhere in the
  resume — that catches this exact duplication pattern, which §10's
  "is this a fabricated new claim" framing wouldn't catch on its own since
  duplicated real content isn't a fabricated claim.
- This may also just resolve itself once §10's fix stops the model from
  appending unrelated sentences at all, rather than needing a wholly
  separate mechanism — evaluate after §10 lands before building more
  detection logic on top.

### 12.1 New hard requirement — enforce single-page output

The user has an explicit, non-negotiable requirement that was never in the
original plan: **the compiled resume must fit on exactly one page, with
no excess white space either.** There is currently no check for this at
all in `/compile` — a 2-page result is returned as if it were a success.

Add this to the backend `/compile` flow: after `compile_tex()` succeeds,
check the resulting PDF's page count (e.g. via `pypdf`, or by shelling out
to a tool already available on this machine like MiKTeX's `pdfinfo`, which
was used during this review to confirm page count). If the result is not
exactly 1 page:
- Do not silently return it as a success. Either reject the compile with a
  clear error surfaced to the extension ("edits produced a 2-page resume;
  uncheck some edits or shorten them and try again"), or automatically
  retry with a stricter length instruction to the model before falling
  back to rejection.
- This is a genuinely new requirement, not just a consequence of §10/§12's
  bugs — even correctly-behaving rephrasing edits could occasionally push
  length over a page in the future, so this check should exist
  independently of whatever fixes §10/§12 land. Don't treat fixing §10 as
  removing the need for this.
- Whatever mechanism is chosen, add a regression test: compile the base
  resume with zero edits (must pass, 1 page) and compile with a
  deliberately oversized fake edit (must be rejected/retried, not silently
  returned as 2 pages).

### 12.2 Bullet-local and one-page enforcement completed (2026-07-16)

Both section 12 blockers are now enforced at the backend boundary without
changing `resume.tex` or `resume.cls`:

- Bullet proposals are still checked word-by-word against their exact target
  bullet. They now also have a conservative growth limit, preventing the
  model from appending an unrelated second sentence while presenting the
  result as a rephrase.
- A separate cross-bullet comparison rejects any substantial contiguous
  phrase copied from a different resume bullet. The exact Game Outcome
  Prediction Platform sibling-bullet duplication reported above is covered
  by a regression test.
- All three reported Flexera fabrications (orchestration/multi-agent tasks,
  agent harnesses, and Agent Manager guardrails/rollback paths) are covered
  and rejected by regression tests.
- `compile_tex()` obtains the generated PDF's page count from MiKTeX/TeX
  Live `pdfinfo`. `/compile` refuses to save or return any result whose page
  count is not exactly one and tells the user to shorten or uncheck edits.
- The unchanged base resume was compiled in isolation and confirmed as one
  page (57,048 bytes). An in-memory oversized edit within the API's 2,000
  character limit compiled to two pages, proving the page counter observes
  the failure case; the API regression proves that result is rejected and
  never written to the output directory.

Verification: 35 Python tests pass, the Firefox background-download binary
regression passes, and both real LaTeX page-count checks pass. The template
files were not modified.
