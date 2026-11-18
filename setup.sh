#!/usr/bin/env bash
# One-time setup: virtualenv, Python deps, CPU torch, ffmpeg check.
set -e
cd "$(dirname "$0")"

echo "==> checking ffmpeg"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "!! ffmpeg is required. Install it first:"
  echo "     Debian/Ubuntu : sudo apt install ffmpeg"
  echo "     Fedora        : sudo dnf install ffmpeg"
  echo "     macOS         : brew install ffmpeg"
  echo "     Windows       : winget install ffmpeg"
  exit 1
fi

echo "==> creating virtualenv (.venv)"
python3 -m venv .venv
. .venv/bin/activate

echo "==> installing python packages"
pip install --upgrade pip
pip install -r requirements.txt
echo "==> installing torch (CPU build)"
pip install --index-url https://download.pytorch.org/whl/cpu torch

mkdir -p models state output examples

cat <<'MSG'

Setup done.

  Start the app : ./run.sh          → http://127.0.0.1:8099
  Command line  : . .venv/bin/activate && python cli.py VIDEO_OR_FOLDER

Models are NOT bundled — they download automatically on first use into
  models/        Whisper speech models
  models/hf/     translation models
MSG
