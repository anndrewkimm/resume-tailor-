import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal

import redis

from . import config
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


_lock = threading.Lock()
_redis_client: redis.Redis | None = None


def _client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis_client


def _key(job_id: str) -> str:
    return f"resume-tailor:job:{job_id}"


def _serialize(job: TailorJob) -> str:
    return json.dumps(
        {
            "kind": job.kind,
            "status": job.status,
            "step": job.step,
            "analysis": job.analysis.model_dump() if job.analysis else None,
            "edits": [edit.model_dump() for edit in job.edits],
            "fit": job.fit.model_dump() if job.fit else None,
            "paragraphs": [paragraph.model_dump() for paragraph in job.paragraphs],
            "error": job.error,
        }
    )


def _deserialize(raw: str) -> TailorJob:
    payload = json.loads(raw)
    return TailorJob(
        kind=payload["kind"],
        status=payload["status"],
        step=payload["step"],
        analysis=(
            ExtractKeywordsResponse.model_validate(payload["analysis"])
            if payload["analysis"]
            else None
        ),
        edits=[ReviewedEdit.model_validate(edit) for edit in payload["edits"]],
        fit=FitReport.model_validate(payload["fit"]) if payload["fit"] else None,
        paragraphs=[
            ReviewedParagraph.model_validate(paragraph)
            for paragraph in payload["paragraphs"]
        ],
        error=payload["error"],
    )


def create_job(kind: Literal["tailor", "letter"] = "tailor") -> str:
    job_id = uuid.uuid4().hex
    step = "Drafting a grounded cover letter…" if kind == "letter" else "Extracting role requirements…"
    job = TailorJob(kind=kind, step=step)
    _client().set(_key(job_id), _serialize(job), ex=config.JOB_STATE_TTL_SECONDS)
    return job_id


def get_job(job_id: str) -> TailorJob | None:
    raw = _client().get(_key(job_id))
    return _deserialize(raw) if raw is not None else None


def update_job(job_id: str, **fields) -> None:
    with _lock:
        job = get_job(job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        _client().set(_key(job_id), _serialize(job), ex=config.JOB_STATE_TTL_SECONDS)
