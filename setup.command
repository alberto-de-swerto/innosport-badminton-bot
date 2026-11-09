#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -f config.json ]]; then
  cp config.example.json config.json
fi
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
echo 'Установка завершена. Откройте login.command для входа.'
read -r '?Нажмите Enter, чтобы закрыть окно: '
