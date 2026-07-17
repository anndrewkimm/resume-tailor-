import re
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import ValidationError

from . import config
from .latex_compile import CompileError, compile_tex
from .llm import LLMError, extract_keywords, generate_edits
from .models import (
    CompileRequest,
    ExtractKeywordsRequest,
    ExtractKeywordsResponse,
    GenerateDiffRequest,
    GenerateDiffResponse,
    ReviewedEdit,
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
    return GenerateDiffResponse(edits=reviewed)


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
