#!/usr/bin/env bash
# One-command start for Linux/macOS hosts: creates a venv, installs requirements, runs the panel.
set -e
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
exec ./venv/bin/python server.py
