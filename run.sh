#!/usr/bin/env bash
# Start Subtitle Studio (sets itself up on first run).
set -e
cd "$(dirname "$0")"
exec python3 bootstrap.py "$@"
