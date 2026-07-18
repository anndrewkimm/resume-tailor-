from .models import FitReport, Keyword, KeywordMatch
from .resume_parser import _phrase_present


_IMPORTANCE_WEIGHT = {"high": 3, "medium": 2, "low": 1}
_IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def compute_fit(resume_source: str, keywords: list[Keyword]) -> FitReport:
    """Return deterministic, whole-resume weighted keyword coverage."""
    if not keywords:
        return FitReport(score=0)

    matched: list[KeywordMatch] = []
    missing: list[KeywordMatch] = []
    matched_weight = 0
    total_weight = 0
    for keyword in keywords:
        weight = _IMPORTANCE_WEIGHT[keyword.importance]
        is_matched = _phrase_present(resume_source, keyword.term)
        total_weight += weight
        if is_matched:
            matched_weight += weight
        item = KeywordMatch(
            term=keyword.term,
            category=keyword.category,
            importance=keyword.importance,
            matched=is_matched,
        )
        (matched if is_matched else missing).append(item)

    sort_key = lambda item: (_IMPORTANCE_ORDER.get(item.importance, 3), item.term.casefold())
    matched.sort(key=sort_key)
    missing.sort(key=sort_key)
    return FitReport(
        score=round(100 * matched_weight / total_weight),
        matched=matched,
        missing=missing,
    )
