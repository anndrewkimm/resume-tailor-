import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
EXTENSION = ROOT / "extension"


class FirefoxExtensionTests(unittest.TestCase):
    def test_manifest_declares_firefox_identity_and_required_apis(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(
            manifest["browser_specific_settings"]["gecko"]["id"],
            "resume-tailor@local.andrewkim",
        )
        self.assertTrue(
            {"activeTab", "scripting", "storage", "downloads", "alarms"}.issubset(
                manifest["permissions"]
            )
        )

    def test_scripts_prefer_firefox_browser_api_with_chrome_fallback(self):
        for filename in ("popup.js", "content.js", "background.js"):
            source = (EXTENSION / filename).read_text(encoding="utf-8")
            self.assertIn("globalThis.browser ?? globalThis.chrome", source)
            self.assertNotRegex(source, r"\bchrome\.(?:storage|tabs|scripting|downloads|runtime)")

    def test_compile_download_is_owned_by_cross_browser_background(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["background"]["scripts"], ["background.js"])
        self.assertEqual(manifest["background"]["service_worker"], "background.js")

        popup = (EXTENSION / "popup.js").read_text(encoding="utf-8")
        background = (EXTENSION / "background.js").read_text(encoding="utf-8")
        self.assertIn("COMPILE_AND_DOWNLOAD", popup)
        self.assertIn("COMPILE_AND_DOWNLOAD", background)
        self.assertNotIn("downloads.download", popup)
        self.assertNotIn("createObjectURL", popup)
        self.assertIn("downloads.download", background)
        self.assertIn("createObjectURL", background)
        self.assertIn("downloads.onChanged", background)
        self.assertIn('"complete", "interrupted"', background)

    def test_local_credentials_load_before_popup(self):
        html = (EXTENSION / "popup.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="config.local.js"'), html.index('src="popup.js"'))

    def test_completed_results_can_be_discarded_without_compiling(self):
        html = (EXTENSION / "popup.html").read_text(encoding="utf-8")
        popup = (EXTENSION / "popup.js").read_text(encoding="utf-8")

        self.assertIn('id="new-tailor"', html)
        self.assertIn('$("#new-tailor").addEventListener("click", resetTailor)', popup)
        self.assertIn('ext.storage.local.remove(["tailorResult", "activeJobId"])', popup)
        self.assertIn('$("#restart").addEventListener("click", resetTailor)', popup)

    def test_tailoring_job_state_survives_background_worker_restart(self):
        popup = (EXTENSION / "popup.js").read_text(encoding="utf-8")
        background = (EXTENSION / "background.js").read_text(encoding="utf-8")

        self.assertIn('"/tailor/start"', background)
        self.assertIn('statusPath: "/tailor/status"', background)
        self.assertIn("ext.alarms.create", background)
        self.assertIn("ext.alarms.onAlarm", background)
        self.assertIn("activeJobId", background)
        self.assertIn("tailorResult", background)
        self.assertNotIn("let tailorState", background)
        self.assertNotIn("GET_TAILOR_STATE", background)
        self.assertNotIn("RESET_TAILOR_STATE", background)
        self.assertNotIn("TAILOR_STATE", popup)
        self.assertIn("ext.storage.onChanged", popup)
        self.assertIn('["tailorResult", "activeJobId"]', popup)

    def test_fit_and_cover_letter_workflow_are_review_gated(self):
        html = (EXTENSION / "popup.html").read_text(encoding="utf-8")
        popup = (EXTENSION / "popup.js").read_text(encoding="utf-8")
        background = (EXTENSION / "background.js").read_text(encoding="utf-8")

        self.assertIn('id="fit-summary"', html)
        self.assertIn("chip-missing", popup)
        self.assertIn('id="draft-letter"', html)
        self.assertIn('id="confirm-letter"', html)
        self.assertIn("START_COVER_LETTER", background)
        self.assertIn("activeLetterJobId", background)
        self.assertIn("letterResult", background)
        self.assertIn("COMPILE_LETTER_AND_DOWNLOAD", popup)


if __name__ == "__main__":
    unittest.main()
