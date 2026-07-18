import unittest
from pathlib import Path

from backend.app.letter import LetterValidationError, render_letter_tex, validate_letter_paragraph
from backend.app.models import Keyword


SOURCE = (Path(__file__).parents[2] / "resume.tex").read_text(encoding="utf-8")


def technology(term: str) -> Keyword:
    return Keyword(term=term, category="technology", importance="high", evidence="required")


class LetterTests(unittest.TestCase):
    def test_numbers_use_whole_resume_grounding(self):
        self.assertEqual(
            validate_letter_paragraph(SOURCE, "I achieved 90 percent accuracy.", [], "Acme", "Engineer"),
            [],
        )
        issues = validate_letter_paragraph(
            SOURCE, "I improved results by 999 percent.", [], "Acme", "Engineer"
        )
        self.assertTrue(any("999" in issue for issue in issues))

    def test_absent_posting_technology_is_flagged(self):
        issues = validate_letter_paragraph(
            SOURCE,
            "I bring Kubernetes experience to this work.",
            [technology("Kubernetes")],
            "Acme",
            "Engineer",
        )
        self.assertTrue(any("Kubernetes" in issue for issue in issues))

    def test_company_and_role_are_removed_before_grounding(self):
        issues = validate_letter_paragraph(
            SOURCE,
            "I am excited to join Kubernetes Labs as a Kubernetes Engineer.",
            [technology("Kubernetes")],
            "Kubernetes Labs",
            "Kubernetes Engineer",
        )
        self.assertEqual(issues, [])

    def test_backslashes_and_controls_are_hard_rejected(self):
        unsafe = ("tab\there", r"\input{secret}", r"safe until \write18{cmd}")
        for text in unsafe:
            with self.subTest(text=text), self.assertRaises(LetterValidationError):
                validate_letter_paragraph(SOURCE, text, [], "Acme", "Engineer")

    def test_prose_specials_are_flags_free_and_escaped(self):
        # Regression for the percent hard-reject bug: the resume's own facts
        # ("90\% accuracy") must be citable as plain prose in a letter.
        for text in (
            "My models achieved 90% accuracy on win/loss classification.",
            "I contributed to R&D efforts across the data_pipeline work.",
            "A stray { brace or $5 figure is prose, not LaTeX.",
        ):
            with self.subTest(text=text):
                issues = validate_letter_paragraph(SOURCE, text, [], "Acme", "Engineer")
                self.assertEqual([issue for issue in issues if "unsafe" in issue], [])
        rendered = render_letter_tex(
            "Acme", "Engineer", ["My models achieved 90% accuracy."]
        )
        self.assertIn(r"90\% accuracy", rendered)

    def test_ungrounded_number_still_flagged_after_narrowing(self):
        issues = validate_letter_paragraph(
            SOURCE, "I improved throughput by 300% last year.", [], "Acme", "Engineer"
        )
        self.assertTrue(any("300" in issue for issue in issues))

    def test_template_escapes_plain_text_values(self):
        rendered = render_letter_tex("R&D", "C# Engineer", ["Delivered 50% & more_value."])
        self.assertIn(r"R\&D", rendered)
        self.assertIn(r"C\# Engineer", rendered)
        self.assertIn(r"50\% \& more\_value", rendered)
        self.assertNotIn("<<BODY>>", rendered)


if __name__ == "__main__":
    unittest.main()
