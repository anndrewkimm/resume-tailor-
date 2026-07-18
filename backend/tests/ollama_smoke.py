"""Exercise the polled Ollama job, safety review, and PDF compile through real HTTP endpoints."""

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


def get_json(path: str, secret: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        headers={"X-Extension-Secret": secret},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
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
        environment["DATA_DIR"] = str(Path(tmp) / "data")
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

            start_body, _ = post("/tailor/start", secret, {"job_text": JOB_TEXT})
            job_id = json.loads(start_body)["job_id"]
            job_deadline = time.monotonic() + 300
            while True:
                status = get_json(f"/tailor/status/{job_id}", secret)
                if status["status"] != "running":
                    break
                # Health must remain responsive while synchronous Ollama calls
                # run in the backend-owned worker thread.
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as response:
                    if json.load(response).get("status") != "ok":
                        raise RuntimeError("health endpoint became unhealthy during tailoring")
                if time.monotonic() >= job_deadline:
                    raise RuntimeError("tailoring job did not finish within 300 seconds")
                time.sleep(1)

            if status["status"] == "error":
                raise RuntimeError(f"tailoring job failed: {status['error']}")
            if not isinstance(status.get("fit", {}).get("score"), int):
                raise RuntimeError("tailoring job did not return a deterministic fit score")
            analysis = {
                "company": status["company"],
                "role": status["role"],
                "keywords": status["keywords"],
            }
            reviewed = status["edits"]
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

            letter_start, _ = post(
                "/cover-letter/start",
                secret,
                {
                    "job_text": JOB_TEXT,
                    "company": analysis["company"],
                    "role": analysis["role"],
                    "keywords": analysis["keywords"],
                },
            )
            letter_job_id = json.loads(letter_start)["job_id"]
            letter_deadline = time.monotonic() + 300
            while True:
                letter_status = get_json(f"/cover-letter/status/{letter_job_id}", secret)
                if letter_status["status"] != "running":
                    break
                if time.monotonic() >= letter_deadline:
                    raise RuntimeError("cover-letter job did not finish within 300 seconds")
                time.sleep(1)
            if letter_status["status"] == "error":
                raise RuntimeError(f"cover-letter job failed: {letter_status['error']}")
            reviewed_paragraphs = letter_status["paragraphs"]
            if not reviewed_paragraphs:
                raise RuntimeError("Ollama produced no cover-letter paragraphs")
            edited_paragraphs = [{"text": item["text"]} for item in reviewed_paragraphs]
            edited_paragraphs[-1]["text"] += " I welcome the opportunity to discuss this role."
            letter_pdf, _ = post(
                "/cover-letter/compile",
                secret,
                {
                    "company": analysis["company"],
                    "role": analysis["role"],
                    "keywords": analysis["keywords"],
                    "paragraphs": edited_paragraphs,
                    "confirmed_by_user": any(item["issues"] for item in reviewed_paragraphs),
                },
                timeout=120,
            )
            if not letter_pdf.startswith(b"%PDF"):
                raise RuntimeError("cover-letter compile endpoint did not return a PDF")
            if health.get("llm_provider") != "ollama":
                raise RuntimeError("health endpoint did not report Ollama")
            print(
                "Local end-to-end smoke passed: "
                f"{len(analysis['keywords'])} keywords, {len(reviewed)} reviewed edits, "
                f"{len(approved)} compiled edits, {len(pdf)} resume PDF bytes, "
                f"{len(reviewed_paragraphs)} reviewed letter paragraphs, {len(letter_pdf)} letter PDF bytes."
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
