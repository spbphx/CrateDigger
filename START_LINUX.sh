#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
      PY="$(command -v "$c")"; break
    fi
  done
  [ -z "$PY" ] && { echo "Нужен Python 3.10+ (sudo apt install python3 python3-venv)"; exit 1; }
  "$PY" -m venv .venv || { echo "Не создаётся venv: sudo apt install python3-venv"; exit 1; }
fi
.venv/bin/python setup_env.py || exit 1
exec .venv/bin/python app.py
