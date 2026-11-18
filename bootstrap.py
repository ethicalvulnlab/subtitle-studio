#!/usr/bin/env python3
"""Cross-platform setup + launcher for Subtitle Studio.

    python bootstrap.py              # set up if needed, then start the app
    python bootstrap.py --setup-only # only prepare the environment
    python bootstrap.py --headless DIR   # batch mode through cli.py

Works the same on Windows, macOS and Linux: it creates the virtualenv,
installs dependencies (including the CPU build of torch), checks ffmpeg and
then launches uvicorn from inside that environment.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, ".venv")
WINDOWS = os.name == "nt"
VENV_PY = os.path.join(VENV, "Scripts", "python.exe") if WINDOWS else os.path.join(VENV, "bin", "python")


def say(msg: str) -> None:
    print(msg, flush=True)


def check_ffmpeg() -> bool:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    for c in candidates:
        if os.path.exists(c):
            os.environ["PATH"] = os.path.dirname(c) + os.pathsep + os.environ.get("PATH", "")
            return True

    say("!! ffmpeg is required but was not found on PATH.\n")
    if WINDOWS:
        say("   Install it, then reopen the terminal:")
        say("     winget install Gyan.FFmpeg")
        say("   or download from https://www.gyan.dev/ffmpeg/builds/ and add its bin\\ folder to PATH.")
    elif sys.platform == "darwin":
        say("   brew install ffmpeg")
    else:
        say("   sudo apt install ffmpeg      (Debian/Ubuntu)")
        say("   sudo dnf install ffmpeg      (Fedora)")
    return False


def pip_install(python: str, args: list[str]) -> None:
    subprocess.check_call([python, "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    subprocess.check_call([python, "-m", "pip", "install", *args])


def ensure_env(force: bool = False) -> str:
    """Create the venv and install dependencies. Returns the venv python."""
    if force and os.path.isdir(VENV):
        say("==> removing old .venv")
        shutil.rmtree(VENV, ignore_errors=True)

    if not os.path.exists(VENV_PY):
        say(f"==> creating virtualenv at {VENV}")
        venv.EnvBuilder(with_pip=True).create(VENV)

    marker = os.path.join(VENV, ".deps-installed")
    if not os.path.exists(marker):
        say("==> installing python packages (this takes a few minutes)")
        pip_install(VENV_PY, ["-r", os.path.join(ROOT, "requirements.txt")])
        say("==> installing torch (CPU build)")
        subprocess.check_call([
            VENV_PY, "-m", "pip", "install", "--index-url",
            "https://download.pytorch.org/whl/cpu", "torch",
        ])
        with open(marker, "w", encoding="utf-8") as f:
            f.write("ok\n")

    for d in ("models", "state", "output", "examples"):
        os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    return VENV_PY


def main() -> int:
    ap = argparse.ArgumentParser(description="Set up and run Subtitle Studio.")
    ap.add_argument("--setup-only", action="store_true", help="prepare everything and exit")
    ap.add_argument("--force", action="store_true", help="rebuild the virtualenv from scratch")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default="8099")
    ap.add_argument("--headless", metavar="PATH", help="batch mode: subtitle a file or folder")
    args = ap.parse_args()

    if not check_ffmpeg():
        return 1

    python = ensure_env(args.force)

    if args.setup_only:
        say("\nSetup complete.")
        say("  Start the app : " + ("run.bat" if WINDOWS else "./run.sh"))
        say("  Or directly   : " + python + " bootstrap.py")
        return 0

    env = dict(os.environ)
    env["HF_HOME"] = os.path.join(ROOT, "models", "hf")
    env["HF_HUB_DISABLE_XET"] = "1"

    if args.headless:
        cmd = [python, os.path.join(ROOT, "cli.py"), args.headless]
    else:
        say(f"\n==> Subtitle Studio on http://{args.host}:{args.port}   (Ctrl+C to stop)\n")
        cmd = [python, "-m", "uvicorn", "app.server:app",
               "--host", args.host, "--port", str(args.port)]

    try:
        return subprocess.call(cmd, cwd=ROOT, env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
