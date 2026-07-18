import os
import secrets
from pathlib import Path


PLACEHOLDER_SECRET = "replace-with-a-long-random-secret"


def _dotenv_value(lines: list[str], key: str) -> str:
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped.split("=", 1)[1].strip()
    return ""


def configure(repo_root: Path | None = None) -> tuple[Path, Path]:
    root = repo_root or Path(os.environ.get("CONFIG_REPO_ROOT", Path(__file__).parents[2]))
    example_path = root / "backend" / ".env.example"
    env_path = root / "backend" / ".env"
    extension_path = root / "extension" / "config.local.js"
    if not example_path.is_file():
        raise FileNotFoundError(f"configuration template not found at {example_path}")

    example_lines = example_path.read_text(encoding="utf-8").splitlines()
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    shared_secret = _dotenv_value(existing_lines, "SHARED_SECRET")
    if not shared_secret or shared_secret == PLACEHOLDER_SECRET:
        shared_secret = secrets.token_urlsafe(32)
    ollama_host = (
        _dotenv_value(existing_lines, "OLLAMA_HOST")
        or _dotenv_value(example_lines, "OLLAMA_HOST")
        or "http://127.0.0.1:11434"
    )
    ollama_model = (
        _dotenv_value(existing_lines, "OLLAMA_MODEL")
        or _dotenv_value(example_lines, "OLLAMA_MODEL")
        or "qwen2.5:7b-instruct"
    )

    rendered: list[str] = []
    for line in example_lines:
        stripped = line.strip()
        if stripped.startswith("SHARED_SECRET="):
            rendered.append(f"SHARED_SECRET={shared_secret}")
        elif stripped.startswith("OLLAMA_HOST="):
            rendered.append(f"OLLAMA_HOST={ollama_host}")
        elif stripped.startswith("OLLAMA_MODEL="):
            rendered.append(f"OLLAMA_MODEL={ollama_model}")
        else:
            rendered.append(line)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    extension_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    extension_path.write_text(
        "globalThis.RESUME_TAILOR_LOCAL = Object.freeze({ "
        f"sharedSecret: '{shared_secret}' }});\n",
        encoding="utf-8",
    )
    return env_path, extension_path


def main() -> int:
    configure()
    print("Local Ollama backend and extension credentials configured. Secret values were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
