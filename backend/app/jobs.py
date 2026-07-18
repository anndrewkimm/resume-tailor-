import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal

from .models import ExtractKeywordsResponse, FitReport, ReviewedEdit, ReviewedParagraph


JobStatus = Literal["running", "done", "error"]


@dataclass
class TailorJob:
    kind: Literal["tailor", "letter"] = "tailor"
    status: JobStatus = "running"
    step: str = "Extracting role requirements…"
    analysis: ExtractKeywordsResponse | None = None
    edits: list[ReviewedEdit] = field(default_factory=list)
    fit: FitReport | None = None
    paragraphs: list[ReviewedParagraph] = field(default_factory=list)
    error: str | None = None


_jobs: dict[str, TailorJob] = {}
_lock = threading.Lock()


def create_job(kind: Literal["tailor", "letter"] = "tailor") -> str:
    job_id = uuid.uuid4().hex
    with _lock:
        step = "Drafting a grounded cover letter…" if kind == "letter" else "Extracting role requirements…"
        _jobs[job_id] = TailorJob(kind=kind, step=step)
    return job_id


def get_job(job_id: str) -> TailorJob | None:
    with _lock:
        return _jobs.get(job_id)


def update_job(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
