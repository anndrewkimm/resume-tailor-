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


def compile_tex(
    tex_content: str,
    *,
    source_name: str = "resume.tex",
    aux_files: list[Path] | None = None,
) -> CompileResult:
    """Compile a LaTeX string to PDF bytes in an isolated directory.

    Resume compilation defaults to a fresh copy of resume.cls. Other callers
    can provide a different source name and explicit auxiliary files.
    Uses -no-shell-escape so a malformed/malicious edit can't execute shell
    commands via \\write18 (PLAN.md 5.6).
    """
    if Path(source_name).name != source_name or not source_name.endswith(".tex"):
        raise ValueError("source_name must be a plain .tex filename")

    files_to_copy = [config.RESUME_CLS_PATH] if aux_files is None else aux_files
    for path in files_to_copy:
        if not path.exists():
            raise FileNotFoundError(f"LaTeX auxiliary file not found at {path}")

    with tempfile.TemporaryDirectory(prefix="resume-tailor-") as tmpdir:
        tmp_path = Path(tmpdir)
        tex_path = tmp_path / source_name
        tex_path.write_text(tex_content, encoding="utf-8")
        for path in files_to_copy:
            shutil.copy(path, tmp_path / path.name)

        log = ""
        for _ in range(2):  # twice, matching PLAN.md 3.2 (harmless even with no refs)
            try:
                proc = subprocess.run(
                    [
                    config.find_pdflatex(),
                    "-no-shell-escape",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                        source_name,
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

        pdf_path = tmp_path / f"{Path(source_name).stem}.pdf"
        if not pdf_path.exists():
            raise CompileError(log)

        return CompileResult(
            pdf_bytes=pdf_path.read_bytes(),
            page_count=_pdf_page_count(pdf_path),
        )
