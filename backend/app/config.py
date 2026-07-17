import os
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

RESUME_TEX_PATH = Path(os.environ.get("RESUME_TEX_PATH", REPO_ROOT / "resume.tex"))
RESUME_CLS_PATH = Path(os.environ.get("RESUME_CLS_PATH", REPO_ROOT / "resume.cls"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", REPO_ROOT / "output"))

# Extension origin allowlist (see PLAN.md 3.2) — set the real extension ID
# once it's loaded unpacked in Chrome (chrome://extensions, dev mode on).
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "300"))

PDFLATEX_TIMEOUT_SECONDS = int(os.environ.get("PDFLATEX_TIMEOUT_SECONDS", "60"))
MAX_JOB_TEXT_CHARS = int(os.environ.get("MAX_JOB_TEXT_CHARS", "100000"))


def find_pdflatex() -> str:
    """Return the configured/local pdflatex executable name or path."""
    configured = os.environ.get("PDFLATEX_PATH")
    if configured:
        return configured

    windows_default = Path.home() / "AppData/Local/Programs/MiKTeX/miktex/bin/x64/pdflatex.exe"
    if windows_default.exists():
        return str(windows_default)
    return "pdflatex"


def find_pdfinfo() -> str:
    """Return the PDF metadata reader used to enforce one-page output."""
    configured = os.environ.get("PDFINFO_PATH")
    if configured:
        return configured

    windows_default = Path.home() / "AppData/Local/Programs/MiKTeX/miktex/bin/x64/pdfinfo.exe"
    if windows_default.exists():
        return str(windows_default)
    return "pdfinfo"
