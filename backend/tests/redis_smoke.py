"""Exercise job-state persistence against a real Redis instance."""

import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import config, jobs
from backend.app.models import ExtractKeywordsResponse, FitReport, Keyword, KeywordMatch


def main() -> None:
    job_id = jobs.create_job()
    analysis = ExtractKeywordsResponse(
        company="Redis Smoke Co",
        role="Engineer",
        keywords=[
            Keyword(
                term="Python",
                category="technology",
                importance="high",
                evidence="Python is required",
            )
        ],
    )
    fit = FitReport(
        score=100,
        matched=[
            KeywordMatch(
                term="Python",
                category="technology",
                importance="high",
                matched=True,
            )
        ],
        missing=[],
    )
    jobs.update_job(job_id, status="done", analysis=analysis, fit=fit)

    jobs._redis_client = None
    stored = jobs.get_job(job_id)
    if stored is None or stored.status != "done":
        raise RuntimeError("Redis job state did not survive a fresh client connection")
    if stored.analysis != analysis or stored.fit != fit:
        raise RuntimeError("Redis job state did not preserve nested model data")

    jobs._client().delete(jobs._key(job_id))
    print(
        "Redis smoke passed: job state and nested model data survived a simulated "
        f"backend restart via {config.REDIS_URL}."
    )


if __name__ == "__main__":
    main()
