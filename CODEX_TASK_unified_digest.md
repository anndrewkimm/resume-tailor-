# Task: one unified local digest combining tracker, discovery, and email suggestions

## Why this task exists (read this first)

PLAN.md §18 recorded a positioning review against popular public job-search
repos (career-ops, ai-job-search, jobsync). The conclusion: this project's
real advantage is being one coherent pipeline (tailor + tracker + discovery
+ email suggestions, sharing one fit-score model) rather than a bundle of
disconnected tools — but right now that's only true in the backend. The
user-facing surface is still three separate artifacts: `tracker.py`'s
`generate_report()` → `output/applications_report.html`, `discovery.py`'s
`generate_report()` → `output/discovered_jobs_report.html`, and
`email_tracker.py`'s `pending`/`_list_pending()`, which is CLI-only and has
no HTML report at all. This task adds a fourth, combining report that pulls
from all three without changing any of them.

## Scope

One new module, `backend/app/digest.py`, reading from the three existing
modules' already-public functions (`tracker.read_applications()`,
`discovery.read_postings()`, `email_tracker.pending_suggestions()`) — zero
changes to `tracker.py`, `discovery.py`, or `email_tracker.py`. No new HTTP
endpoint: every existing report in this codebase is a local file the user
opens directly, never reachable from the browser extension, and this one
follows the same rule for the same reason (§3.2's stance on not widening
what an unauthenticated browser tab could reach).

**On charts — deliberately minimal, and why.** This isn't a rich
interactive dashboard; it's a static file a Python script writes once, in
the same self-contained-`<style>`-block-no-JS style `tracker.py`/
`discovery.py`'s reports already use (`tracker.py:167-200`,
`discovery.py:329-367`). The one visual addition — a horizontal bar per
funnel stage — needs exactly one color, not a palette: every bar is the
same hue, only its length (share of the largest stage's count) and its
direct-labeled count vary. That's a single-series categorical-by-position
comparison, which is the one chart case that legitimately doesn't need a
legend, hover layer, or palette validation (all of which apply once you
have *multiple* colors to keep distinguishable — irrelevant when there's
only one). Reuse this codebase's own existing report accent,
`#175c35` (already the link/accent color in both existing reports), rather
than introducing a new one. Do not add JavaScript, a charting library, or
a CDN dependency — if a future task wants real multi-series comparisons
here, that's a bigger decision (interactivity, the dataviz skill's full
procedure) and out of scope for this one.

## Implementation

### `backend/app/digest.py` (new)

```python
import html
from collections import Counter
from pathlib import Path

from . import config, discovery, email_tracker, tracker


_FUNNEL_ORDER = ["compiled", "letter", "applied", "screen", "interview", "offer", "rejected", "ghosted"]
_ACCENT = "#175c35"


def _safe_url(value: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "#"
    return value


def _funnel_section(applications: list[dict]) -> str:
    counts = Counter(item.get("status", "compiled") for item in applications)
    present = [status for status in _FUNNEL_ORDER if counts.get(status)]
    if not present:
        return "<p>No applications tracked yet.</p>"
    peak = max(counts[status] for status in present)
    rows = []
    for status in present:
        count = counts[status]
        width = round(100 * count / peak) if peak else 0
        rows.append(
            f'<div class="bar-row"><span class="bar-label">{html.escape(status.title())}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{width}%"></span></span>'
            f'<span class="bar-count">{count}</span></div>'
        )
    return "".join(rows)


def _top_postings_section(postings: list[dict], limit: int = 5) -> str:
    candidates = sorted(
        (item for item in postings if not item.get("status")),
        key=lambda item: int(item.get("fit_score") or 0),
        reverse=True,
    )
    if not candidates:
        return "<li>No unactioned postings — run <code>python -m backend.app.discovery poll</code>.</li>"
    rows = []
    for item in candidates[:limit]:
        company = html.escape(str(item.get("company", "")))
        role = html.escape(str(item.get("role", "")))
        url = html.escape(_safe_url(str(item.get("url", ""))), quote=True)
        fit = "—" if item.get("fit_score") is None else f"{int(item['fit_score'])}%"
        rows.append(f'<li><a href="{url}">{company} — {role}</a> · fit {fit}</li>')
    return "".join(rows)


def _pending_suggestions_section(suggestions: list[dict], limit: int = 5) -> str:
    if not suggestions:
        return "<li>No pending email suggestions.</li>"
    rows = []
    for item in suggestions[:limit]:
        company = html.escape(str(item.get("company", "")))
        role = html.escape(str(item.get("role", "")))
        status = html.escape(str(item.get("status", "")))
        id_prefix = html.escape(str(item.get("id", ""))[:12])
        rows.append(
            f"<li><code>{id_prefix}</code> {company} — {role}: suggested "
            f"<strong>{status}</strong> — confirm with "
            f"<code>python -m backend.app.email_tracker confirm {id_prefix}</code></li>"
        )
    return "".join(rows)


def generate_digest(output_path: Path | None = None) -> Path:
    applications = tracker.read_applications()
    postings = discovery.read_postings()
    suggestions = email_tracker.pending_suggestions()

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Job search digest</title><style>
body{{font:15px system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#1c2a21}}
h1{{margin-bottom:4px}}
h2{{margin-top:32px;font-size:16px}}
a{{color:{_ACCENT}}}
ul{{padding-left:20px}}
code{{background:#eef2eb;padding:1px 5px;border-radius:4px;font-size:13px}}
.bar-row{{display:flex;align-items:center;gap:10px;margin:6px 0}}
.bar-label{{width:100px;flex:none}}
.bar-track{{flex:1;background:#e8f0e9;border-radius:4px;height:14px;overflow:hidden}}
.bar-fill{{display:block;height:100%;background:{_ACCENT};border-radius:4px}}
.bar-count{{width:30px;flex:none;text-align:right}}
</style></head><body>
<h1>Job search digest</h1>
<p>Generated locally from <code>applications.jsonl</code>, <code>discovered_jobs.jsonl</code>, and <code>email_suggestions.jsonl</code>. Nothing here is sent anywhere.</p>
<h2>Application funnel</h2>
{_funnel_section(applications)}
<h2>Top unactioned discovered postings</h2>
<ul>{_top_postings_section(postings)}</ul>
<h2>Pending email suggestions</h2>
<ul>{_pending_suggestions_section(suggestions)}</ul>
</body></html>"""
    destination = output_path or (config.OUTPUT_DIR / "digest_report.html")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination


def _configure_console_encoding() -> None:
    import sys
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    destination = generate_digest()
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Move the two local `import` statements (`urllib.parse`, `sys`) to the
module top alongside the others — written inline above only to keep each
function's copy-paste block self-contained; the actual file should follow
this codebase's existing style of imports at the top (see any other module
in `backend/app/`).

`_safe_url` duplicates `discovery._safe_report_url` (`discovery.py:322-326`)
almost exactly. Don't import the private helper across modules; a four-line
duplicate is cheaper than coupling two report-generators through a private
name. `_configure_console_encoding` duplicates the same six-line helper
already in `discovery.py:409-419` and `email_tracker.py:405-413` for the
same reason — this is the third copy, which is a reasonable point to
notice but not a reason to refactor mid-task; if a future task touches any
of these three again, promoting it to one shared tiny helper module is
worth doing then.

Note `_top_postings_section` intentionally filters to postings with no
`status` (i.e. neither `dismissed` nor `tailored`, matching
`discovery.py`'s own `--new-only` semantics at `discovery.py:388-407`) —
showing already-handled postings in a digest meant to prompt action would
be noise.

### `README.md`

Add a short "Job search digest" section after "Email tracking (optional)",
one command:

```powershell
python -m backend.app.digest
```

Writes `output/digest_report.html`. One line noting it's a read-only
combination of the other three reports — running it never changes any of
the three underlying JSONL logs.

## Verification

New `backend/tests/test_digest.py`, mirroring the existing report tests'
style (`test_generate_report_sorts_scores_and_escapes_cells_and_url` in
`test_discovery.py`, `test_report_html_escapes_untrusted_fields` in
`test_tracker.py`):

- Empty state: no applications/postings/suggestions at all → the three
  empty-state messages appear, no exception.
- Funnel bars: seed applications across a few statuses, assert each
  present status renders with a `bar-fill` width proportional to its count
  relative to the largest bucket (e.g. peak status → `width:100%`), and a
  status with zero applications does not render a row at all.
- Top postings: seed postings with and without a `status`; assert only
  unactioned ones appear, sorted by `fit_score` descending, capped at 5,
  and that an untrusted `company`/`role`/`url` (script tags, javascript:
  URL) is HTML-escaped / neutralized to `#` exactly like
  `discovery.generate_report`'s existing test already covers for its own
  report.
- Pending suggestions: seed via `email_tracker._record_suggested`, assert
  it appears with the right `confirm` command hint; assert a resolved
  (confirmed/dismissed) suggestion does not appear (relies on
  `pending_suggestions()`'s own filtering, already tested in
  `test_email_tracker.py` — this test only needs to confirm the digest
  renders whatever `pending_suggestions()` returns, not re-test the
  filtering itself).
- `python -m unittest discover -s backend/tests -v` passes.
- Manual: with some real local data in all three JSONL logs, run
  `python -m backend.app.digest`, open `output/digest_report.html`, confirm
  it renders sensibly and the bar widths visually match the funnel counts.
