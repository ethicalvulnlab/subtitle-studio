#!/usr/bin/env bash
# Start the Arabic subtitle builder UI.
set -e
cd "$(dirname "$0")"
. .venv/bin/activate
export HF_HOME="$PWD/models/hf"
export HF_HUB_DISABLE_XET=1
exec uvicorn app.server:app --host 127.0.0.1 --port 8099 "$@"
