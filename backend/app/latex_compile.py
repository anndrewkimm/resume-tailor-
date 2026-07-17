import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config


class CompileError(Exception):
    def __init__(self, log: str):
        super().__init__("LaTeX compile failed")
        self.log = log


@dataclass
class CompileResult:
    pdf_bytes: bytes
    page_count: int = 1


def _pdf_page_count(pdf_path: Path) -> int:
    """Read the compiler output's page count with MiKTeX/TeX Live pdfinfo."""
    try:
        proc = subprocess.run(
            [config.find_pdfinfo(), str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=config.PDFLATEX_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise CompileError(
            "pdfinfo was not found. Install MiKTeX/TeX Live or set PDFINFO_PATH; "
            "page count verification is required."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CompileError("PDF page count verification timed out") from exc
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise CompileError("PDF page count verification failed:\n" + output)
    match = re.search(r"^Pages:\s+(\d+)\s*$", output, re.MULTILINE | re.IGNORECASE)
    if not match:
        raise CompileError("pdfinfo did not report a page count:\n" + output)
    return int(match.group(1))


def compile_tex(tex_content: str) -> CompileResult:
    """Compile a resume.tex string to PDF bytes.

    Always compiles in an isolated temp directory with a fresh copy of
    resume.cls, never touching the real resume.tex on disk (PLAN.md 3.2/3.4).
    Uses -no-shell-escape so a malformed/malicious edit can't execute shell
    commands via \\write18 (PLAN.md 5.6).
    """
    if not config.RESUME_CLS_PATH.exists():
        raise FileNotFoundError(f"resume.cls not found at {config.RESUME_CLS_PATH}")

    with tempfile.TemporaryDirectory(prefix="resume-tailor-") as tmpdir:
        tmp_path = Path(tmpdir)
        tex_path = tmp_path / "resume.tex"
        tex_path.write_text(tex_content, encoding="utf-8")
        shutil.copy(config.RESUME_CLS_PATH, tmp_path / "resume.cls")

        log = ""
        for _ in range(2):  # twice, matching PLAN.md 3.2 (harmless even with no refs)
            try:
                proc = subprocess.run(
                    [
                    config.find_pdflatex(),
                    "-no-shell-escape",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "resume.tex",
                    ],
                    cwd=tmp_path,
                    capture_output=True,
                    text=True,
                    timeout=config.PDFLATEX_TIMEOUT_SECONDS,
                )
            except FileNotFoundError as exc:
                raise CompileError(
                    "pdflatex was not found. Install MiKTeX/TeX Live or set PDFLATEX_PATH."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise CompileError(f"LaTeX compile timed out after {config.PDFLATEX_TIMEOUT_SECONDS}s") from exc
            log = proc.stdout + proc.stderr
            if proc.returncode != 0:
                raise CompileError(log)

        pdf_path = tmp_path / "resume.pdf"
        if not pdf_path.exists():
            raise CompileError(log)

        return CompileResult(
            pdf_bytes=pdf_path.read_bytes(),
            page_count=_pdf_page_count(pdf_path),
        )
