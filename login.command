#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Сначала откройте setup.command.'
else
  .venv/bin/python sportsite.py login
  .venv/bin/python sportsite.py check
fi
read -r '?Нажмите Enter, чтобы закрыть окно: '
