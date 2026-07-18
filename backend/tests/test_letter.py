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

    def test_latex_unsafe_text_is_hard_rejected(self):
        unsafe = ("tab\there", "unbalanced { brace", r"\input{secret}", "raw 50% claim")
        for text in unsafe:
            with self.subTest(text=text), self.assertRaises(LetterValidationError):
                validate_letter_paragraph(SOURCE, text, [], "Acme", "Engineer")

    def test_template_escapes_plain_text_values(self):
        rendered = render_letter_tex("R&D", "C# Engineer", ["Delivered 50% & more_value."])
        self.assertIn(r"R\&D", rendered)
        self.assertIn(r"C\# Engineer", rendered)
        self.assertIn(r"50\% \& more\_value", rendered)
        self.assertNotIn("<<BODY>>", rendered)


if __name__ == "__main__":
    unittest.main()
