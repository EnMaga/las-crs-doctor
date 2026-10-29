#!/usr/bin/env bash
# LAS CRS Doctor launcher for Linux/macOS. First run creates .venv and installs the tool.
set -e
cd "$(dirname "$0")"
if [ ! -f .venv/setup.ok ]; then
  PY=$(command -v python3 || command -v python)
  "$PY" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 9) else 1)" \
    || { echo "Need Python >= 3.9 with Tkinter (Debian/Ubuntu: sudo apt install python3-tk python3-venv)"; exit 1; }
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e .
  touch .venv/setup.ok
fi
exec .venv/bin/python -m lascrs
