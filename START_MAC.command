#!/bin/bash
# CrateDigger — запуск на macOS (двойной клик; в первый раз: правый клик → Открыть)
cd "$(dirname "$0")" || exit 1
echo ""
echo "  CrateDigger — копание семплов для бум-бэпа"
echo ""

if [ ! -x ".venv/bin/python" ]; then
  PY=""
  for c in /Library/Frameworks/Python.framework/Versions/3.1{3,2,1,4,0}/bin/python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$(command -v "$c")"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Нужен Python 3.10 или новее."
    echo "Сейчас откроется python.org — скачайте «macOS 64-bit universal installer», установите"
    echo "и снова запустите START_MAC.command."
    open "https://www.python.org/downloads/macos/"
    read -r -p "Нажмите Enter, чтобы закрыть..."
    exit 1
  fi
  echo "Создаю окружение Python в этой папке..."
  "$PY" -m venv .venv || { read -r -p "Ошибка. Enter..."; exit 1; }
fi

.venv/bin/python setup_env.py || { read -r -p "Ошибка установки. Enter..."; exit 1; }
exec .venv/bin/python app.py
