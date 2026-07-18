import unittest

from backend.app.fit import compute_fit
from backend.app.models import Keyword


def keyword(term: str, importance: str) -> Keyword:
    return Keyword(
        term=term,
        category="technology",
        importance=importance,
        evidence="test",
    )


class FitTests(unittest.TestCase):
    def test_weighted_coverage_and_stable_lists(self):
        report = compute_fit(
            "Experienced with Python and SQL.",
            [keyword("Docker", "high"), keyword("SQL", "low"), keyword("Python", "medium")],
        )
        self.assertEqual(report.score, 50)
        self.assertEqual([item.term for item in report.matched], ["Python", "SQL"])
        self.assertEqual([item.term for item in report.missing], ["Docker"])
        self.assertTrue(all(item.matched for item in report.matched))

    def test_phrase_matcher_actual_alias_behavior_is_preserved(self):
        report = compute_fit("JavaScript APIs", [keyword("JS", "high"), keyword("API", "low")])
        self.assertEqual([item.term for item in report.missing], ["JS", "API"])
        self.assertEqual(report.matched, [])

    def test_empty_keywords_returns_zero(self):
        report = compute_fit("Python", [])
        self.assertEqual(report.score, 0)
        self.assertEqual(report.matched, [])
        self.assertEqual(report.missing, [])


if __name__ == "__main__":
    unittest.main()
