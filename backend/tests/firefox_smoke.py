"""Install the extension temporarily in an isolated headless Firefox profile."""

import asyncio
import json
import os
import shutil
import socket as network_socket
import subprocess
import tempfile
import time
from pathlib import Path

import websockets


ROOT = Path(__file__).parents[2]
EXTENSION = ROOT / "extension"
FIREFOX = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Mozilla Firefox/firefox.exe"


async def command(socket, command_id: int, method: str, params: dict) -> dict:
    await socket.send(json.dumps({"id": command_id, "method": method, "params": params}))
    while True:
        try:
            raw_response = await asyncio.wait_for(socket.recv(), timeout=20)
        except TimeoutError as error:
            raise RuntimeError(f"timed out waiting for Firefox response to {method}") from error
        response = json.loads(raw_response)
        if response.get("id") != command_id:
            continue
        if response.get("type") == "error":
            raise RuntimeError(f"{method} failed: {response.get('error')}: {response.get('message')}")
        return response["result"]


async def install_extension(port: int) -> str:
    deadline = time.monotonic() + 20
    while True:
        try:
            socket = await websockets.connect(f"ws://127.0.0.1:{port}/session", proxy=None)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Firefox WebDriver BiDi endpoint did not start")
            await asyncio.sleep(0.25)

    async with socket:
        await command(
            socket,
            1,
            "session.new",
            {"capabilities": {"alwaysMatch": {"browserName": "firefox"}}},
        )
        result = await command(
            socket,
            2,
            "webExtension.install",
            {
                "extensionData": {"type": "path", "path": str(EXTENSION.resolve())},
                "moz:permanent": False,
            },
        )
        extension_id = result["extension"]
        await command(socket, 3, "webExtension.uninstall", {"extension": extension_id})
        # Firefox closes the BiDi socket while ending the session and does not
        # consistently send a final response. The outer process cleanup owns
        # shutdown, so do not wait indefinitely for that optional reply.
        await socket.send(json.dumps({"id": 4, "method": "session.end", "params": {}}))
        return extension_id


def main() -> None:
    if not FIREFOX.is_file():
        raise RuntimeError(f"Firefox not found at {FIREFOX}")
    if not (EXTENSION / "config.local.js").is_file():
        raise RuntimeError("extension/config.local.js is missing; run configure.cmd first")

    profile = Path(tempfile.mkdtemp(prefix="resume-tailor-firefox-"))
    with network_socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            str(FIREFOX),
            "--no-remote",
            "--headless",
            "--profile",
            str(profile),
            "--remote-debugging-port",
            str(port),
            "--remote-allow-system-access",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    try:
        extension_id = asyncio.run(install_extension(port))
        if extension_id != "resume-tailor@local.andrewkim":
            raise RuntimeError(f"unexpected installed extension id: {extension_id}")
        print(f"Firefox accepted and installed the temporary extension: {extension_id}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for _ in range(10):
            try:
                shutil.rmtree(profile)
                break
            except PermissionError:
                time.sleep(0.2)


if __name__ == "__main__":
    main()
