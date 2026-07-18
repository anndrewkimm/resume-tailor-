import re
from pathlib import Path

from . import config
from .models import Keyword
from .resume_parser import _known_entities, _phrase_present


class LetterValidationError(ValueError):
    pass


_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")


def _without_analysis_labels(text: str, company: str, role: str) -> str:
    cleaned = text
    for label in (company, role):
        if label:
            cleaned = re.sub(re.escape(label), " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def validate_letter_paragraph(
    resume_source: str,
    text: str,
    keywords: list[Keyword],
    company: str,
    role: str,
) -> list[str]:
    """Validate model/user prose against resume-global factual grounding."""
    value = text.strip()
    # Only backslashes (command smuggling, PLAN.md 5.6) and control characters
    # are hard failures. Braces and %&_#$ are legitimate prose ("90% accuracy",
    # "R&D") and are fully neutralized by escape_latex before compilation.
    hard_failures: list[str] = []
    if any(ord(char) < 32 for char in value):
        hard_failures.append("contains control characters")
    if "\\" in value:
        hard_failures.append("contains a forbidden LaTeX command or backslash")
    if hard_failures:
        raise LetterValidationError("; ".join(hard_failures))

    candidate_text = _without_analysis_labels(value, company, role)
    issues: list[str] = []
    resume_numbers = set(_NUMBER_PATTERN.findall(resume_source))
    for number in _NUMBER_PATTERN.findall(candidate_text):
        if number not in resume_numbers:
            issues.append(f"numeric claim is not grounded in the resume: {number}")

    candidates = _known_entities(resume_source) | {keyword.term for keyword in keywords}
    for candidate in sorted(candidates, key=str.casefold):
        if _phrase_present(candidate_text, candidate) and not _phrase_present(resume_source, candidate):
            issues.append(f"entity or posting keyword is not grounded in the resume: {candidate}")
    return list(dict.fromkeys(issues))


def escape_latex(value: str) -> str:
    marker = "\u0000BACKSLASH\u0000"
    escaped = value.replace("\\", marker)
    replacements = {
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    for source, replacement in replacements.items():
        escaped = escaped.replace(source, replacement)
    return escaped.replace(marker, r"\textbackslash{}")


def render_letter_tex(company: str, role: str, paragraphs: list[str]) -> str:
    template_path = Path(config.REPO_ROOT) / "cover_letter_template.tex"
    if not template_path.is_file():
        raise FileNotFoundError(f"cover letter template not found at {template_path}")
    body = "\n\n\\par\n\n".join(escape_latex(paragraph.strip()) for paragraph in paragraphs)
    return (
        template_path.read_text(encoding="utf-8")
        .replace("<<COMPANY>>", escape_latex(company))
        .replace("<<ROLE>>", escape_latex(role))
        .replace("<<BODY>>", body)
    )
