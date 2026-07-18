import re
import sys
import threading
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import ValidationError

from . import config, tracker
from .fit import compute_fit
from .jobs import create_job, get_job, update_job
from .letter import LetterValidationError, render_letter_tex, validate_letter_paragraph
from .latex_compile import CompileError, compile_tex
from .llm import LLMError, draft_cover_letter, extract_keywords, generate_edits
from .models import (
    CompileCoverLetterRequest,
    CompileRequest,
    CoverLetterStatusResponse,
    ExtractKeywordsRequest,
    ExtractKeywordsResponse,
    GenerateDiffRequest,
    GenerateDiffResponse,
    ReviewedEdit,
    ReviewedParagraph,
    StartCoverLetterRequest,
    StartTailorRequest,
    StartTailorResponse,
    TailorStatusResponse,
)
from .resume_parser import ResumeEditError, apply_edits, validate_edit
from .security import require_extension_origin

app = FastAPI(title="Resume Tailor Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(?:chrome-extension|moz-extension)://[^/]+$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Extension-Secret"],
    expose_headers=["Content-Disposition"],
)


def _base_resume() -> str:
    if not config.RESUME_TEX_PATH.is_file():
        raise HTTPException(status_code=500, detail=f"resume.tex not found at {config.RESUME_TEX_PATH}")
    return config.RESUME_TEX_PATH.read_text(encoding="utf-8")


def _check_job_size(job_text: str) -> None:
    if len(job_text) > config.MAX_JOB_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"job posting exceeds the {config.MAX_JOB_TEXT_CHARS:,} character limit",
        )


def _safe_filename_part(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value)
    return "_".join(words)[:80] or "Unknown"


def _review_edits(source: str, proposals, keyword_terms: list[str]) -> list[ReviewedEdit]:
    reviewed: list[ReviewedEdit] = []
    seen: set[tuple[str, str, int | None]] = set()
    for proposal in proposals:
        key = (proposal.target.section, proposal.target.anchor, proposal.target.item_index)
        try:
            original, issues = validate_edit(source, proposal, keyword_terms)
        except ResumeEditError as exc:
            original, issues = "", [str(exc)]
        if key in seen:
            issues.append("duplicate target")
        seen.add(key)
        reviewed.append(
            ReviewedEdit(
                **proposal.model_dump(),
                original_text=original,
                traceable=not issues,
                issues=list(dict.fromkeys(issues)),
            )
        )
    return reviewed


def _run_tailor_job(job_id: str, job_text: str) -> None:
    try:
        analysis = extract_keywords(job_text)
        source = _base_resume()
        fit = compute_fit(source, analysis.keywords)
        update_job(
            job_id,
            step="Drafting grounded resume edits…",
            analysis=analysis,
            fit=fit,
        )
        proposals = generate_edits(job_text, analysis.keywords, source)
        keyword_terms = [keyword.term for keyword in analysis.keywords if keyword.category == "technology"]
        reviewed = _review_edits(source, proposals, keyword_terms)
        update_job(job_id, status="done", analysis=analysis, edits=reviewed, fit=fit)
    except (LLMError, ValidationError) as exc:
        update_job(job_id, status="error", error=str(exc))
    except Exception as exc:
        # Last-resort guard: no job should remain stuck at "running" forever.
        update_job(job_id, status="error", error=f"Unexpected error: {exc}")


def _run_cover_letter_job(
    job_id: str,
    job_text: str,
    company: str,
    role: str,
    keywords,
) -> None:
    try:
        source = _base_resume()
        draft = draft_cover_letter(job_text, company, role, keywords, source)
        paragraphs = []
        for paragraph in draft.paragraphs:
            # A hard failure in one drafted paragraph must not error the whole
            # job — flag it so the user can edit that paragraph in review.
            # /cover-letter/compile still rejects unsafe text with 422.
            try:
                issues = validate_letter_paragraph(
                    source, paragraph.text, keywords, company, role
                )
            except LetterValidationError as exc:
                issues = [f"unsafe: {exc}"]
            paragraphs.append(ReviewedParagraph(text=paragraph.text, issues=issues))
        update_job(job_id, status="done", paragraphs=paragraphs)
    except (LLMError, ValidationError) as exc:
        update_job(job_id, status="error", error=str(exc))
    except Exception as exc:
        update_job(job_id, status="error", error=f"Unexpected error: {exc}")


@app.post("/extract-keywords", response_model=ExtractKeywordsResponse)
def extract(req: ExtractKeywordsRequest, _: None = Depends(require_extension_origin)) -> ExtractKeywordsResponse:
    _check_job_size(req.job_text)
    try:
        return extract_keywords(req.job_text)
    except (LLMError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/generate-diff", response_model=GenerateDiffResponse)
def generate(req: GenerateDiffRequest, _: None = Depends(require_extension_origin)) -> GenerateDiffResponse:
    _check_job_size(req.job_text)
    source = _base_resume()
    try:
        proposals = generate_edits(req.job_text, req.keywords, source)
    except (LLMError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    keyword_terms = [keyword.term for keyword in req.keywords if keyword.category == "technology"]
    return GenerateDiffResponse(edits=_review_edits(source, proposals, keyword_terms))


@app.post("/tailor/start", response_model=StartTailorResponse)
def start_tailor(
    req: StartTailorRequest, _: None = Depends(require_extension_origin)
) -> StartTailorResponse:
    _check_job_size(req.job_text)
    job_id = create_job()
    threading.Thread(target=_run_tailor_job, args=(job_id, req.job_text), daemon=True).start()
    return StartTailorResponse(job_id=job_id)


@app.get("/tailor/status/{job_id}", response_model=TailorStatusResponse)
def tailor_status(
    job_id: str, _: None = Depends(require_extension_origin)
) -> TailorStatusResponse:
    job = get_job(job_id)
    if job is None or job.kind != "tailor":
        raise HTTPException(status_code=404, detail="unknown job_id")
    return TailorStatusResponse(
        status=job.status,
        step=job.step,
        company=job.analysis.company if job.analysis else None,
        role=job.analysis.role if job.analysis else None,
        keywords=job.analysis.keywords if job.analysis else [],
        edits=job.edits,
        fit=job.fit,
        error=job.error,
    )


@app.post("/cover-letter/start", response_model=StartTailorResponse)
def start_cover_letter(
    req: StartCoverLetterRequest, _: None = Depends(require_extension_origin)
) -> StartTailorResponse:
    _check_job_size(req.job_text)
    job_id = create_job(kind="letter")
    threading.Thread(
        target=_run_cover_letter_job,
        args=(job_id, req.job_text, req.company, req.role, req.keywords),
        daemon=True,
    ).start()
    return StartTailorResponse(job_id=job_id)


@app.get("/cover-letter/status/{job_id}", response_model=CoverLetterStatusResponse)
def cover_letter_status(
    job_id: str, _: None = Depends(require_extension_origin)
) -> CoverLetterStatusResponse:
    job = get_job(job_id)
    if job is None or job.kind != "letter":
        raise HTTPException(status_code=404, detail="unknown job_id")
    return CoverLetterStatusResponse(
        status=job.status,
        step=job.step,
        paragraphs=job.paragraphs,
        error=job.error,
    )


@app.post("/compile")
def compile_resume(req: CompileRequest, _: None = Depends(require_extension_origin)) -> Response:
    source = _base_resume()
    problems: list[str] = []
    keyword_terms = [keyword.term for keyword in req.keywords if keyword.category == "technology"]
    for index, edit in enumerate(req.approved_edits):
        try:
            _, issues = validate_edit(source, edit, keyword_terms)
            problems.extend(f"edit {index + 1}: {issue}" for issue in issues)
        except ResumeEditError as exc:
            problems.append(f"edit {index + 1}: {exc}")
    if problems:
        raise HTTPException(status_code=422, detail={"message": "unsafe or invalid edits rejected", "issues": problems})

    try:
        tex_content = apply_edits(source, req.approved_edits)
        result = compile_tex(tex_content)
    except ResumeEditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (CompileError, FileNotFoundError) as exc:
        log = exc.log if isinstance(exc, CompileError) else str(exc)
        raise HTTPException(status_code=422, detail={"message": "LaTeX compile failed", "log": log}) from exc

    if result.page_count != 1:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"Edits produced a {result.page_count}-page resume; exactly one page is required. "
                    "Uncheck or shorten some edits and try again."
                )
            },
        )

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"Resume_{_safe_filename_part(req.company)}_{_safe_filename_part(req.role)}.pdf"
    (config.OUTPUT_DIR / filename).write_bytes(result.pdf_bytes)
    try:
        fit = compute_fit(source, req.keywords)
        tracker.record_compiled(
            company=req.company,
            role=req.role,
            filename=filename,
            edits_applied=len(req.approved_edits),
            fit_score=fit.score,
            keywords_total=len(fit.matched) + len(fit.missing),
            keywords_matched=len(fit.matched),
        )
    except Exception as exc:
        print(f"warning: could not record compiled application: {exc}", file=sys.stderr)
    return Response(
        content=result.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/cover-letter/compile")
def compile_cover_letter(
    req: CompileCoverLetterRequest, _: None = Depends(require_extension_origin)
) -> Response:
    source = _base_resume()
    grounding_issues: list[str] = []
    safety_issues: list[str] = []
    for index, paragraph in enumerate(req.paragraphs):
        try:
            issues = validate_letter_paragraph(
                source, paragraph.text, req.keywords, req.company, req.role
            )
            grounding_issues.extend(f"paragraph {index + 1}: {issue}" for issue in issues)
        except LetterValidationError as exc:
            safety_issues.append(f"paragraph {index + 1}: {exc}")
    if safety_issues:
        raise HTTPException(
            status_code=422,
            detail={"message": "unsafe cover-letter text rejected", "issues": safety_issues},
        )
    if grounding_issues and not req.confirmed_by_user:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "cover-letter claims require explicit user confirmation",
                "issues": grounding_issues,
            },
        )

    try:
        tex_content = render_letter_tex(
            req.company, req.role, [paragraph.text for paragraph in req.paragraphs]
        )
        result = compile_tex(tex_content, source_name="cover_letter.tex", aux_files=[])
    except (CompileError, FileNotFoundError, ValueError) as exc:
        log = exc.log if isinstance(exc, CompileError) else str(exc)
        raise HTTPException(
            status_code=422,
            detail={"message": "LaTeX compile failed", "log": log},
        ) from exc
    if result.page_count != 1:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"The cover letter is {result.page_count} pages; exactly one page is required. "
                    "Shorten it and try again."
                )
            },
        )

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"CoverLetter_{_safe_filename_part(req.company)}_{_safe_filename_part(req.role)}.pdf"
    (config.OUTPUT_DIR / filename).write_bytes(result.pdf_bytes)
    try:
        tracker.record_letter(company=req.company, role=req.role, filename=filename)
    except Exception as exc:
        print(f"warning: could not record cover letter: {exc}", file=sys.stderr)
    return Response(
        content=result.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "time": datetime.now(UTC).isoformat(),
        "resume_found": config.RESUME_TEX_PATH.is_file(),
        "llm_provider": "ollama",
        "ollama_model": config.OLLAMA_MODEL,
    }
