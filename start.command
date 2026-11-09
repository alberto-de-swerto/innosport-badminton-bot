#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Сначала откройте setup.command.'
  read -r '?Нажмите Enter, чтобы закрыть окно: '
  exit 1
fi
.venv/bin/python sportsite.py run
