import tempfile
import unittest
from pathlib import Path

from backend.app.configure import PLACEHOLDER_SECRET, configure


class ConfigureTests(unittest.TestCase):
    def test_generates_matching_secret_without_replacing_existing_one(self):
        with tempfile.TemporaryDirectory(prefix="resume-config-tests-") as tmpdir:
            root = Path(tmpdir)
            (root / "backend").mkdir()
            (root / "extension").mkdir()
            (root / "backend" / ".env.example").write_text(
                f"SHARED_SECRET={PLACEHOLDER_SECRET}\n"
                "OLLAMA_HOST=http://127.0.0.1:11434\n"
                "OLLAMA_MODEL=test-model\n",
                encoding="utf-8",
            )
            env_path, extension_path = configure(root)
            first_env = env_path.read_text(encoding="utf-8")
            self.assertNotIn(PLACEHOLDER_SECRET, first_env)
            secret = first_env.split("SHARED_SECRET=", 1)[1].splitlines()[0]
            self.assertIn(secret, extension_path.read_text(encoding="utf-8"))

            configure(root)
            self.assertIn(f"SHARED_SECRET={secret}", env_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
