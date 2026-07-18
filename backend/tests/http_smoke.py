"""Run an authenticated compile through a real local uvicorn process."""

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
PORT = 18765


def env_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""


def main() -> None:
    secret = env_value(ROOT / "backend/.env", "SHARED_SECRET")
    if not secret:
        raise RuntimeError("SHARED_SECRET is not configured")

    with tempfile.TemporaryDirectory(prefix="resume-tailor-http-") as tmp:
        environment = os.environ.copy()
        environment["OUTPUT_DIR"] = tmp
        environment["DATA_DIR"] = str(Path(tmp) / "data")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
            ],
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

            body = json.dumps(
                {"company": "Firefox", "role": "Smoke Test", "approved_edits": [], "keywords": []}
            ).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/compile",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "X-Extension-Secret": secret},
            )
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    pdf = response.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"compile endpoint returned HTTP {exc.code}: {detail}") from exc
            if health.get("status") != "ok" or not pdf.startswith(b"%PDF"):
                raise RuntimeError("backend smoke test returned an invalid response")
            if not list(Path(tmp).glob("*.pdf")):
                raise RuntimeError("compiled PDF was not written to the isolated output directory")
            print("Authenticated HTTP backend compile returned a valid PDF from an isolated output directory.")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
