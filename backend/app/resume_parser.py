import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import EditTarget, ProposedEdit


class ResumeEditError(ValueError):
    pass


@dataclass(frozen=True)
class LocatedText:
    text: str
    spans: tuple[tuple[int, int], ...]


def _balanced_content(source: str, opening_brace: int) -> tuple[str, int]:
    if opening_brace >= len(source) or source[opening_brace] != "{":
        raise ResumeEditError("expected opening brace")
    depth = 0
    escaped = False
    for index in range(opening_brace, len(source)):
        char = source[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening_brace + 1 : index], index
    raise ResumeEditError("unbalanced braces in resume")


def _section_bounds(source: str, section: str) -> tuple[int, int]:
    match = re.search(r"\\begin\{rSection\}\{" + re.escape(section) + r"\}", source, re.IGNORECASE)
    if not match:
        raise ResumeEditError(f"section '{section}' not found")
    end = source.find(r"\end{rSection}", match.end())
    if end < 0:
        raise ResumeEditError(f"section '{section}' is not closed")
    return match.end(), end


def _subsection_bounds(source: str, section: str, anchor: str) -> tuple[int, int]:
    section_start, section_end = _section_bounds(source, section)
    pattern = re.compile(r"\\begin\{rSubsection\}\{([^{}]+)\}")
    matches = [m for m in pattern.finditer(source, section_start, section_end) if m.group(1).strip() == anchor.strip()]
    if len(matches) != 1:
        raise ResumeEditError(f"expected one subsection '{anchor}' in {section}, found {len(matches)}")
    end = source.find(r"\end{rSubsection}", matches[0].end(), section_end)
    if end < 0:
        raise ResumeEditError(f"subsection '{anchor}' is not closed")
    return matches[0].end(), end


def _textmd_spans(source: str, start: int, end: int, prefix_pattern: str) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    for match in re.finditer(prefix_pattern, source[start:end]):
        opening = start + match.end() - 1
        content, closing = _balanced_content(source, opening)
        if closing <= end:
            found.append((content, opening + 1, closing))
    return found


def locate_target(source: str, target: EditTarget) -> LocatedText:
    if target.section != "Technologies":
        start, end = _subsection_bounds(source, target.section, target.anchor)
        bullets = _textmd_spans(source, start, end, r"\\item\s+\\textmd\{")
        assert target.item_index is not None
        if target.item_index >= len(bullets):
            raise ResumeEditError(
                f"bullet {target.item_index} not found under {target.section}/{target.anchor}"
            )
        content, span_start, span_end = bullets[target.item_index]
        return LocatedText(content, ((span_start, span_end),))

    allowed = {"Languages", "Frameworks & Libraries", "Tools & Platforms"}
    if target.anchor not in allowed:
        raise ResumeEditError(f"unknown Technologies row '{target.anchor}'")
    start, end = _section_bounds(source, "Technologies")
    latex_label = target.anchor.replace("&", r"\&")
    label = re.search(re.escape(latex_label) + r":", source[start:end])
    if not label:
        raise ResumeEditError(f"technology row '{target.anchor}' not found")
    value_start = start + label.end()
    next_label = re.search(r"(?:Languages|Frameworks\s+\\&\s+Libraries|Tools\s+\\&\s+Platforms):", source[value_start:end])
    value_end = value_start + next_label.start() if next_label else end
    pieces = _textmd_spans(source, value_start, value_end, r"\\textmd\{")
    if not pieces:
        raise ResumeEditError(f"technology row '{target.anchor}' has no value")
    values = [piece.strip().rstrip(",") for piece, _, _ in pieces]
    return LocatedText(", ".join(value for value in values if value), tuple((a, b) for _, a, b in pieces))


def bullet_catalog(source: str) -> list[tuple[EditTarget, str]]:
    """Return every editable bullet with its stable address."""
    catalog: list[tuple[EditTarget, str]] = []
    pattern = re.compile(r"\\begin\{rSubsection\}\{([^{}]+)\}")
    for section in ("Experience", "Projects"):
        section_start, section_end = _section_bounds(source, section)
        for subsection in pattern.finditer(source, section_start, section_end):
            anchor = subsection.group(1).strip()
            start, end = _subsection_bounds(source, section, anchor)
            bullets = _textmd_spans(source, start, end, r"\\item\s+\\textmd\{")
            for index, (text, _, _) in enumerate(bullets):
                catalog.append(
                    (EditTarget(section=section, anchor=anchor, item_index=index), text)
                )
    return catalog


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_skill(value: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", "", value.lower())


_NONFACTUAL_WORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "with",
    "across", "including", "through", "using", "used", "while", "that", "which", "their", "its",
    "built", "created", "delivered", "designed", "developed", "enabled", "enhanced", "implemented", "improved",
    "integrated", "leveraged", "provided", "supported", "utilized", "optimized", "streamlined", "focused",
    "automated", "allowing", "enabling", "ensuring", "providing", "supporting", "resulting",
}


def _plain_latex(value: str) -> str:
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"\\textbf\{([^{}]*)\}", r"\1", value)
    value = value.replace(r"\%", "%").replace(r"\&", "&").replace(r"\_", "_")
    value = re.sub(r"\\[A-Za-z]+", " ", value)
    return value.replace("{", " ").replace("}", " ")


def _word_tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9]*(?:[.+#-][A-Za-z0-9+#]+)*", _plain_latex(value).lower())


def _token_variants(token: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9+#]+", "", token.lower())
    aliases = {"js": "javascript", "apis": "api", "restapis": "restapi"}
    normalized = aliases.get(normalized, normalized)
    variants = {normalized}
    for suffix in ("ing", "ed", "es", "s"):
        if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 4:
            variants.add(normalized[: -len(suffix)])
    return {variant for variant in variants if variant}


def _phrase_present(text: str, phrase: str) -> bool:
    haystack = [_normalize_skill(token) for token in _word_tokens(text)]
    needle = [_normalize_skill(token) for token in _word_tokens(phrase)]
    if not needle:
        return False
    width = len(needle)
    return any(haystack[index : index + width] == needle for index in range(len(haystack) - width + 1))


def _known_entities(source: str) -> set[str]:
    entities = {match.strip() for match in re.findall(r"\\textbf\{([^{}]+)\}", source) if match.strip()}
    for anchor in ("Languages", "Frameworks & Libraries", "Tools & Platforms"):
        target = EditTarget(section="Technologies", anchor=anchor, item_index=None)
        entities.update(_split_csv(locate_target(source, target).text))
    return entities


def _copied_bullet_issue(source: str, edit: ProposedEdit, original: str, value: str) -> str | None:
    """Detect a substantial phrase copied from a different resume bullet."""
    proposed_tokens = _word_tokens(value)
    original_tokens = _word_tokens(original)
    target_key = (edit.target.section, edit.target.anchor, edit.target.item_index)
    for other_target, other_text in bullet_catalog(source):
        other_key = (other_target.section, other_target.anchor, other_target.item_index)
        if other_key == target_key:
            continue
        other_tokens = _word_tokens(other_text)
        match = SequenceMatcher(None, proposed_tokens, other_tokens, autojunk=False).find_longest_match()
        if match.size < 8:
            continue
        copied = proposed_tokens[match.a : match.a + match.size]
        copied_text = " ".join(copied)
        if _phrase_present(" ".join(original_tokens), copied_text):
            continue
        return (
            "copies content from a different resume bullet: "
            f"{other_target.section}/{other_target.anchor} bullet {other_target.item_index}"
        )
    return None


def validate_edit(source: str, edit: ProposedEdit, keyword_terms: list[str] | None = None) -> tuple[str, list[str]]:
    located = locate_target(source, edit.target)
    issues: list[str] = []
    value = edit.new_text.strip()

    if any(ord(char) < 32 for char in value):
        issues.append("contains control characters")
    depth = 0
    for char in value:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if depth < 0:
            break
    if depth != 0:
        issues.append("contains unbalanced braces")

    commands = re.findall(r"\\([A-Za-z]+|[%&_])", value)
    forbidden = sorted({command for command in commands if command not in {"textbf", "%", "&", "_"}})
    if forbidden:
        issues.append("contains forbidden LaTeX command(s): " + ", ".join(forbidden))
    if re.search(r"(?<!\\)[%&_#$]", value):
        issues.append("contains an unescaped LaTeX special character")

    if edit.target.section == "Technologies":
        old_skills = {_normalize_skill(item) for item in _split_csv(located.text)}
        new_skills = {_normalize_skill(item) for item in _split_csv(value)}
        if old_skills != new_skills or len(_split_csv(located.text)) != len(_split_csv(value)):
            issues.append("technology rows may only be reordered; membership changed")
        return located.text, issues

    original = located.text
    copied_issue = _copied_bullet_issue(source, edit, original, value)
    if copied_issue:
        issues.append(copied_issue)

    original_word_count = len(_word_tokens(original))
    proposed_word_count = len(_word_tokens(value))
    permitted_growth = max(6, (original_word_count + 3) // 4)
    if proposed_word_count > original_word_count + permitted_growth:
        issues.append(
            "is substantially longer than the original target bullet; rephrase that bullet only"
        )

    original_numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", original))
    for number in re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", value):
        if number not in original_numbers:
            issues.append(f"numeric claim is not grounded in the original target bullet: {number}")

    # Known technologies and emphasized factual phrases cannot be moved from
    # another bullet into this one; that would create a false relationship.
    candidates = _known_entities(source) | set(keyword_terms or [])
    for candidate in sorted(candidates, key=str.lower):
        if _phrase_present(value, candidate) and not _phrase_present(original, candidate):
            issues.append(f"entity is not grounded in the original target bullet: {candidate}")

    # Plain-text local-model edits no longer contain bold spans. Conservatively
    # require every newly introduced content word to exist in the original
    # target bullet. A small allowlist covers connective/editorial language;
    # uncertain rephrasing is flagged for review instead of silently compiled.
    original_variants: set[str] = set()
    for token in _word_tokens(original):
        original_variants.update(_token_variants(token))
    ungrounded: list[str] = []
    for token in _word_tokens(value):
        if token in _NONFACTUAL_WORDS:
            continue
        if not (_token_variants(token) & original_variants):
            ungrounded.append(token)
    if ungrounded:
        issues.append(
            "introduces terms not grounded in the original target bullet: "
            + ", ".join(dict.fromkeys(ungrounded))
        )

    return located.text, list(dict.fromkeys(issues))


def apply_edits(source: str, edits: list[ProposedEdit]) -> str:
    targets = [(e.target.section, e.target.anchor, e.target.item_index) for e in edits]
    if len(targets) != len(set(targets)):
        raise ResumeEditError("duplicate edit targets are not allowed")

    result = source
    for edit in edits:
        located = locate_target(result, edit.target)
        if edit.target.section != "Technologies":
            start, end = located.spans[0]
            result = result[:start] + edit.new_text.strip() + result[end:]
            continue

        new_items = _split_csv(edit.new_text)
        old_counts = [len(_split_csv(result[start:end])) for start, end in located.spans]
        cursor = 0
        replacements: list[tuple[int, int, str]] = []
        for index, (start, end) in enumerate(located.spans):
            count = old_counts[index] if index < len(old_counts) - 1 else len(new_items) - cursor
            chunk = new_items[cursor : cursor + count]
            cursor += count
            suffix = "," if index < len(located.spans) - 1 else ""
            replacements.append((start, end, ", ".join(chunk) + suffix))
        for start, end, replacement in reversed(replacements):
            result = result[:start] + replacement + result[end:]
    return result
