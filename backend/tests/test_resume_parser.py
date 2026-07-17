import unittest
from pathlib import Path

from backend.app.models import EditTarget, ProposedEdit
from backend.app.resume_parser import ResumeEditError, apply_edits, bullet_catalog, locate_target, validate_edit


SOURCE = (Path(__file__).parents[2] / "resume.tex").read_text(encoding="utf-8")


def edit(section: str, anchor: str, text: str, item_index: int | None = None) -> ProposedEdit:
    return ProposedEdit(
        target=EditTarget(section=section, anchor=anchor, item_index=item_index),
        new_text=text,
        reason="test",
    )


class ResumeParserTests(unittest.TestCase):
    def test_catalogs_all_editable_bullets(self):
        catalog = bullet_catalog(SOURCE)
        self.assertEqual(len(catalog), 11)
        self.assertEqual(catalog[0][0].section, "Experience")
        self.assertEqual(catalog[-1][0].anchor, "Real-Time Multiplayer Game with Firebase Integration")

    def test_locates_nested_bullet_content(self):
        target = EditTarget(section="Experience", anchor="Flexera", item_index=0)
        value = locate_target(SOURCE, target).text
        self.assertIn(r"\textbf{Microsoft Copilot Studio}", value)
        self.assertIn(r"\textbf{Freshservice}", value)

    def test_locates_wrapped_technology_row(self):
        target = EditTarget(section="Technologies", anchor="Frameworks & Libraries")
        value = locate_target(SOURCE, target).text
        self.assertEqual(
            value,
            "Pytorch, Scikit-learn, React, Node.js, Express.js, Tailwind CSS, Numpy, XGBoost, Pandas",
        )

    def test_allows_rephrase_with_existing_entities(self):
        proposal = edit(
            "Projects",
            "Game Outcome Prediction Platform",
            r"Delivered a \textbf{90\% accuracy} \textbf{XGBoost machine learning system} for player outcomes.",
            0,
        )
        _, issues = validate_edit(SOURCE, proposal, ["XGBoost", "machine learning"])
        self.assertEqual(issues, [])

    def test_flags_new_entity_and_forbidden_command(self):
        proposal = edit(
            "Projects",
            "Game Outcome Prediction Platform",
            r"Deployed with \textbf{Docker}. \input{secrets}",
            0,
        )
        _, issues = validate_edit(SOURCE, proposal, ["Docker"])
        self.assertTrue(any("Docker" in issue for issue in issues))
        self.assertTrue(any("input" in issue for issue in issues))

    def test_rejects_fabricated_agent_manager_guardrails_and_rollback(self):
        fabrications = (
            "Contributed to the orchestration layer by designing workflows that route and sequence multi-agent tasks.",
            "Extended agent harnesses by building tooling for agents to test and validate their outputs.",
            "Contributed to the Agent Manager system by implementing guardrails and rollback paths.",
        )
        for fabrication in fabrications:
            with self.subTest(fabrication=fabrication):
                proposal = edit("Experience", "Flexera", fabrication, 0)
                _, issues = validate_edit(SOURCE, proposal)
                self.assertTrue(issues)
                self.assertTrue(any("not grounded" in issue for issue in issues))

    def test_rejects_entity_moved_from_another_bullet(self):
        proposal = edit(
            "Experience",
            "Flexera",
            r"Built workflows with \textbf{Firebase} integration.",
            0,
        )
        _, issues = validate_edit(SOURCE, proposal)
        self.assertTrue(any("Firebase" in issue for issue in issues))

    def test_rejects_number_borrowed_from_another_bullet(self):
        proposal = edit(
            "Experience",
            "Flexera",
            r"Improved workflow accuracy by \textbf{90\%}.",
            0,
        )
        _, issues = validate_edit(SOURCE, proposal)
        self.assertTrue(any("numeric claim" in issue for issue in issues))

    def test_rejects_verbatim_sibling_bullet_appended_to_project_bullet(self):
        first = locate_target(
            SOURCE,
            EditTarget(
                section="Projects",
                anchor="Game Outcome Prediction Platform",
                item_index=0,
            ),
        ).text
        sibling = locate_target(
            SOURCE,
            EditTarget(
                section="Projects",
                anchor="Game Outcome Prediction Platform",
                item_index=1,
            ),
        ).text
        proposal = edit(
            "Projects",
            "Game Outcome Prediction Platform",
            f"{first} {sibling}",
            0,
        )
        _, issues = validate_edit(SOURCE, proposal)
        self.assertTrue(any("copies content from a different resume bullet" in issue for issue in issues))

    def test_rejects_appended_fabricated_sentence_by_length_and_terms(self):
        original = locate_target(
            SOURCE, EditTarget(section="Experience", anchor="Flexera", item_index=0)
        ).text
        proposal = edit(
            "Experience",
            "Flexera",
            original
            + " Contributed to the orchestration layer by designing workflows that route and sequence multi-agent tasks.",
            0,
        )
        _, issues = validate_edit(SOURCE, proposal)
        self.assertTrue(any("not grounded" in issue for issue in issues))
        self.assertTrue(any("substantially longer" in issue for issue in issues))

    def test_flags_control_characters_and_unescaped_latex_specials(self):
        proposal = edit(
            "Projects",
            "Game Outcome Prediction Platform",
            "Improved\taccuracy to 90% with raw_data.",
            0,
        )
        _, issues = validate_edit(SOURCE, proposal)
        self.assertTrue(any("control" in issue for issue in issues))
        self.assertTrue(any("unescaped" in issue for issue in issues))

    def test_technology_membership_is_immutable(self):
        reordered = edit(
            "Technologies",
            "Languages",
            "Python, SQL, C++, Java, JavaScript, Swift, HTML, CSS",
        )
        _, issues = validate_edit(SOURCE, reordered)
        self.assertEqual(issues, [])

        added = edit(
            "Technologies",
            "Languages",
            "Python, SQL, C++, Java, JavaScript, Swift, HTML, CSS, Go",
        )
        _, issues = validate_edit(SOURCE, added)
        self.assertTrue(issues)

    def test_applies_bullet_and_wrapped_technology_reordering(self):
        bullet = edit(
            "Experience",
            "Flexera",
            r"Built traceable workflows with \textbf{Power Automate} and \textbf{REST API} integrations.",
            1,
        )
        skills = edit(
            "Technologies",
            "Frameworks & Libraries",
            "React, Node.js, Pytorch, Scikit-learn, Express.js, Tailwind CSS, Numpy, XGBoost, Pandas",
        )
        changed = apply_edits(SOURCE, [bullet, skills])
        self.assertEqual(locate_target(changed, bullet.target).text, bullet.new_text)
        self.assertEqual(locate_target(changed, skills.target).text, skills.new_text)

    def test_rejects_duplicate_targets(self):
        proposal = edit("Experience", "Flexera", "First", 0)
        with self.assertRaises(ResumeEditError):
            apply_edits(SOURCE, [proposal, proposal])


if __name__ == "__main__":
    unittest.main()
