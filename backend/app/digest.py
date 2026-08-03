import argparse
import html
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import config, discovery, email_tracker, tracker


_ACTIVITY_DAYS = 30
_PIPELINE_STATUSES = (
    "compiled",
    "letter",
    "applied",
    "screen",
    "interview",
    "offer",
    "rejected",
    "ghosted",
)
_SERIES = (
    ("Discovered", "#176c49"),
    ("Applications", "#315c9e"),
    ("Email suggestions", "#a15c16"),
)


def _today() -> date:
    return datetime.now(UTC).date()


def _safe_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "#"
    return value


def _event_date(value: object) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _activity_counts(
    postings: list[dict],
    applications: list[dict],
    suggestions: list[dict],
) -> tuple[list[date], list[list[int]]]:
    days = [
        _today() - timedelta(days=offset)
        for offset in reversed(range(_ACTIVITY_DAYS))
    ]
    day_indexes = {day: index for index, day in enumerate(days)}
    series = [[0 for _ in days] for _ in _SERIES]
    for series_index, items in enumerate((postings, applications, suggestions)):
        for item in items:
            item_date = _event_date(item.get("at"))
            if item_date in day_indexes:
                series[series_index][day_indexes[item_date]] += 1
    return days, series


def _activity_chart(
    postings: list[dict],
    applications: list[dict],
    suggestions: list[dict],
) -> str:
    days, series = _activity_counts(postings, applications, suggestions)
    width, height = 760, 250
    left, right, top, bottom = 44, 16, 18, 38
    plot_width = width - left - right
    plot_height = height - top - bottom
    maximum = max((value for values in series for value in values), default=0)
    maximum = max(maximum, 1)

    def point(index: int, value: int) -> tuple[float, float]:
        x = left + (plot_width * index / max(len(days) - 1, 1))
        y = top + plot_height - (plot_height * value / maximum)
        return x, y

    grid = []
    for step in range(5):
        value = round(maximum * step / 4)
        y = top + plot_height - (plot_height * step / 4)
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            'stroke="#dce2d9" stroke-width="1" />'
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="10" fill="#667168">{value}</text>'
        )

    lines = []
    for (label, color), values in zip(_SERIES, series, strict=True):
        points = " ".join(
            f"{x:.1f},{y:.1f}" for x, y in (point(index, value) for index, value in enumerate(values))
        )
        lines.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" '
            f'stroke-width="2.5" stroke-linejoin="round" aria-label="{label}" />'
        )
    start_label = html.escape(days[0].strftime("%b %d"))
    end_label = html.escape(days[-1].strftime("%b %d"))
    description = ", ".join(
        f"{label}: {sum(values)}"
        for (label, _), values in zip(_SERIES, series, strict=True)
    )
    legend = "".join(
        f'<span><i style="background:{color}"></i>{html.escape(label)}</span>'
        for label, color in _SERIES
    )
    return f"""<div class="chart-wrap">
<svg class="activity-chart" viewBox="0 0 {width} {height}" role="img" aria-labelledby="activity-title activity-desc">
<title id="activity-title">Thirty-day job-search activity</title>
<desc id="activity-desc">{html.escape(description)}</desc>
{''.join(grid)}
{''.join(lines)}
<text x="{left}" y="{height - 10}" font-size="11" fill="#667168">{start_label}</text>
<text x="{width - right}" y="{height - 10}" text-anchor="end" font-size="11" fill="#667168">{end_label}</text>
</svg><div class="legend">{legend}</div></div>"""


def _funnel_section(applications: list[dict]) -> str:
    counts = Counter(str(item.get("status") or "compiled") for item in applications)
    present = [status for status in _PIPELINE_STATUSES if counts[status]]
    if not present:
        return "<p>No applications tracked yet.</p>"
    maximum = max(counts[status] for status in present)
    rows = []
    for status in present:
        count = counts[status]
        width = 100 * count / maximum
        rows.append(
            '<li>'
            f'<span class="pipeline-label">{html.escape(status.title())}</span>'
            f'<span class="pipeline-track"><span class="pipeline-bar" style="width:{width:.1f}%"></span></span>'
            f'<strong>{count}</strong>'
            '</li>'
        )
    return f'<ul class="pipeline" aria-label="Current application outcomes">{"".join(rows)}</ul>'


def _discovery_rows(postings: list[dict]) -> str:
    actionable = [posting for posting in postings if not posting.get("status")]
    actionable.sort(key=lambda item: str(item.get("at", "")), reverse=True)
    actionable.sort(
        key=lambda item: (
            item.get("fit_score") is not None,
            int(item.get("fit_score") or 0),
        ),
        reverse=True,
    )
    rows = []
    for item in actionable[:5]:
        fit = "Unscored" if item.get("fit_score") is None else f"{int(item['fit_score'])}%"
        url = html.escape(_safe_url(str(item.get("url", ""))), quote=True)
        company = html.escape(str(item.get("company", "")))
        role = html.escape(str(item.get("role", "")))
        location = html.escape(str(item.get("location") or "—"))
        rows.append(
            f'<tr><td><strong>{fit}</strong></td><td>{company}</td>'
            f'<td><a href="{url}">{role}</a></td><td>{location}</td></tr>'
        )
    return "".join(rows) or '<tr><td colspan="4">No unhandled discovered jobs.</td></tr>'


def _application_rows(applications: list[dict]) -> str:
    recent = sorted(
        applications,
        key=lambda item: str(item.get("outcome_at") or item.get("at", "")),
        reverse=True,
    )
    rows = []
    for item in recent[:20]:
        at = html.escape(str(item.get("at", ""))[:10])
        company = html.escape(str(item.get("company", "")))
        role = html.escape(str(item.get("role", "")))
        fit = "—" if item.get("fit_score") is None else f"{int(item['fit_score'])}%"
        status = html.escape(str(item.get("status") or "compiled").title())
        rows.append(
            f"<tr><td>{at}</td><td>{company}</td><td>{role}</td>"
            f"<td>{fit}</td><td>{status}</td></tr>"
        )
    return "".join(rows) or '<tr><td colspan="5">No applications tracked yet.</td></tr>'


def _suggestion_rows(suggestions: list[dict]) -> str:
    rows = []
    for item in sorted(suggestions, key=lambda value: str(value.get("at", "")), reverse=True):
        suggestion_id = html.escape(str(item.get("id", ""))[:12])
        company = html.escape(str(item.get("company", "")))
        role = html.escape(str(item.get("role", "")))
        status = html.escape(str(item.get("status", "")).title())
        evidence = html.escape(str(item.get("evidence", ""))[:200])
        command = f"python -m backend.app.email_tracker confirm {suggestion_id}"
        rows.append(
            f"<tr><td><code>{suggestion_id}</code></td><td>{company}</td>"
            f"<td>{role}</td><td>{status}</td><td>{evidence}</td>"
            f"<td><code>{command}</code></td></tr>"
        )
    return "".join(rows) or '<tr><td colspan="6">No email suggestions need review.</td></tr>'


def generate_digest(output_path: Path | None = None) -> Path:
    postings = discovery.read_postings()
    applications = tracker.read_applications()
    suggestions = email_tracker.pending_suggestions()
    new_count = sum(not posting.get("status") for posting in postings)
    scored = [int(posting["fit_score"]) for posting in postings if posting.get("fit_score") is not None]
    average_fit = "—" if not scored else f"{round(sum(scored) / len(scored))}%"
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Job-search digest</title><style>
:root{{--ink:#17211b;--muted:#667168;--paper:#f7f8f3;--card:#fff;--line:#dce2d9;--green:#176c49}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1180px;margin:36px auto;padding:0 20px 50px}}h1{{margin-bottom:4px;font:700 30px/1.15 Georgia,serif}}
h2{{margin:28px 0 10px}}.muted{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.card,.panel{{border:1px solid var(--line);border-radius:10px;background:var(--card)}}.card{{padding:16px}}
.card strong{{display:block;font-size:25px;color:var(--green)}}.panel{{padding:16px;overflow:auto}}
.two-col{{display:grid;grid-template-columns:2fr 1fr;gap:14px}}table{{width:100%;border-collapse:collapse}}
th,td{{padding:9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{font-size:11px;text-transform:uppercase;color:var(--muted)}}
a{{color:#175c35}}code{{font-size:12px}}.activity-chart{{display:block;width:100%;min-width:520px;height:auto}}
.chart-wrap{{overflow-x:auto}}.legend{{display:flex;gap:18px;flex-wrap:wrap;margin:4px 0 0 44px;color:var(--muted)}}
.legend span{{display:flex;align-items:center;gap:6px}}.legend i{{width:18px;height:3px;display:inline-block}}
.pipeline{{list-style:none;margin:0;padding:0}}.pipeline li{{display:grid;grid-template-columns:82px 1fr 28px;gap:8px;align-items:center;margin:10px 0}}
.pipeline-label{{font-size:12px}}.pipeline-track{{height:12px;border-radius:99px;background:#edf0ea;overflow:hidden}}
.pipeline-bar{{display:block;height:100%;border-radius:99px;background:var(--green)}}
@media(max-width:760px){{.cards{{grid-template-columns:repeat(2,1fr)}}.two-col{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>Job-search digest</h1><p class="muted">Generated {generated_at}. Local data only.</p>
<section class="cards" aria-label="Summary">
<div class="card"><span>New postings</span><strong>{new_count}</strong></div>
<div class="card"><span>Tracked applications</span><strong>{len(applications)}</strong></div>
<div class="card"><span>Pending email reviews</span><strong>{len(suggestions)}</strong></div>
<div class="card"><span>Average discovered fit</span><strong>{average_fit}</strong></div>
</section>
<section class="two-col"><div><h2>30-day activity</h2><div class="panel">{_activity_chart(postings, applications, suggestions)}</div></div>
<div><h2>Application pipeline</h2><div class="panel">{_funnel_section(applications)}</div></div></section>
<section><h2>Discovered jobs to review</h2><div class="panel"><table><thead><tr><th>Fit</th><th>Company</th><th>Role</th><th>Location</th></tr></thead><tbody>{_discovery_rows(postings)}</tbody></table></div></section>
<section><h2>Recent applications</h2><div class="panel"><table><thead><tr><th>Date</th><th>Company</th><th>Role</th><th>Fit</th><th>Status</th></tr></thead><tbody>{_application_rows(applications)}</tbody></table></div></section>
<section><h2>Email suggestions awaiting confirmation</h2><p class="muted">Review with <code>python -m backend.app.email_tracker pending</code>; this report never confirms an outcome.</p>
<div class="panel"><table><thead><tr><th>ID</th><th>Company</th><th>Role</th><th>Suggested status</th><th>Evidence</th><th>Confirm command</th></tr></thead><tbody>{_suggestion_rows(suggestions)}</tbody></table></div></section>
</main></body></html>"""
    destination = output_path or (config.OUTPUT_DIR / "digest_report.html")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the unified local job-search digest")
    parser.parse_args(argv)
    print(generate_digest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
