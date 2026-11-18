#!/usr/bin/env bash
# One-time setup for Subtitle Studio.
set -e
cd "$(dirname "$0")"
exec python3 bootstrap.py --setup-only
