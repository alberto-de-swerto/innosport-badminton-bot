#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Сначала открой setup.command.'
else
  .venv/bin/python sportsite.py telegram-setup || true
fi
read -r '?Нажми Enter, чтобы закрыть окно: '
