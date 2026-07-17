"""Exercise Ollama, safety review, and PDF compile through real HTTP endpoints."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).parents[2]
PORT = 18766
JOB_TEXT = """Example Analytics is hiring a Software Engineering Intern to build data products and
machine-learning services. The intern will develop Python and SQL data pipelines, integrate REST APIs and
JSON data sources, build React and Node.js user experiences, and apply machine learning techniques such as
feature engineering, cross-validation, and XGBoost. Candidates should be comfortable with Git, API testing,
data processing, and communicating results. Experience deploying reliable analytics workflows is preferred."""


def env_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""


def post(path: str, secret: str, payload: dict, timeout: int = 300) -> tuple[bytes, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Extension-Secret": secret},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return body, dict(response.headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} returned HTTP {exc.code}: {detail}") from exc


def main() -> None:
    secret = env_value(ROOT / "backend/.env", "SHARED_SECRET")
    if not secret:
        raise RuntimeError("SHARED_SECRET is not configured; run configure.cmd")

    with tempfile.TemporaryDirectory(prefix="resume-tailor-ollama-") as tmp:
        environment = os.environ.copy()
        environment["OUTPUT_DIR"] = tmp
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as response:
                        health = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("backend did not become healthy")
                    time.sleep(0.25)

            keyword_body, _ = post("/extract-keywords", secret, {"job_text": JOB_TEXT})
            analysis = json.loads(keyword_body)
            diff_body, _ = post(
                "/generate-diff",
                secret,
                {"job_text": JOB_TEXT, "keywords": analysis["keywords"]},
            )
            reviewed = json.loads(diff_body)["edits"]
            approved = [
                {key: edit[key] for key in ("target", "new_text", "reason")}
                for edit in reviewed
                if edit["traceable"]
            ]
            if not approved:
                raise RuntimeError("Ollama produced no traceable edits")

            pdf, _ = post(
                "/compile",
                secret,
                {
                    "company": analysis["company"],
                    "role": analysis["role"],
                    "keywords": analysis["keywords"],
                    "approved_edits": approved,
                },
                timeout=120,
            )
            if not pdf.startswith(b"%PDF"):
                raise RuntimeError("compile endpoint did not return a PDF")
            if health.get("llm_provider") != "ollama":
                raise RuntimeError("health endpoint did not report Ollama")
            print(
                "Local end-to-end smoke passed: "
                f"{len(analysis['keywords'])} keywords, {len(reviewed)} reviewed edits, "
                f"{len(approved)} compiled edits, {len(pdf)} PDF bytes."
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
