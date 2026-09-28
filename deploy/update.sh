#!/usr/bin/env bash
# Pulls the latest code, reinstalls requirements if they changed, and
# restarts both services. Run from the repo root (or anywhere — it cds in).

set -euo pipefail

APP_DIR="/home/ec2-user/app"
cd "$APP_DIR"

OLD_HASH="$(sha256sum requirements.txt 2>/dev/null || true)"

echo "==> Pulling latest code"
git pull --ff-only

NEW_HASH="$(sha256sum requirements.txt 2>/dev/null || true)"

if [ "$OLD_HASH" != "$NEW_HASH" ]; then
  echo "==> requirements.txt changed — reinstalling"
  "$APP_DIR/.venv/bin/pip" install -r requirements.txt
else
  echo "==> requirements.txt unchanged — skipping reinstall"
fi

echo "==> Restarting services"
sudo systemctl restart sales-intellect-flask sales-intellect-streamlit

sudo systemctl status sales-intellect-flask --no-pager
sudo systemctl status sales-intellect-streamlit --no-pager
