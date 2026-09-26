#!/usr/bin/env bash
# One-time provisioning for a fresh Ubuntu 24.04 EC2 instance (arm64 or
# amd64). Safe to re-run: every step checks current state before acting.
#
# Assumes this repo has already been cloned to /home/ubuntu/app and this
# script is being run as the `ubuntu` user (it uses sudo for privileged
# steps) from inside that checkout, e.g.:
#
#   git clone <repo-url> /home/ubuntu/app
#   cd /home/ubuntu/app
#   cp .env.example .env && nano .env   # fill in real values
#   ./deploy/setup.sh

set -euo pipefail

APP_DIR="/home/ubuntu/app"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$SCRIPT_DIR" != "$APP_DIR/deploy" ]; then
  echo "This script expects the repo to be checked out at $APP_DIR (found it at $(dirname "$SCRIPT_DIR"))." >&2
  echo "Move the checkout there, or edit APP_DIR and the deploy/*.service files to match." >&2
  exit 1
fi

if [ "$(whoami)" != "ubuntu" ]; then
  echo "Run this as the ubuntu user (the systemd units use User=ubuntu)." >&2
  exit 1
fi

cd "$APP_DIR"

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl ca-certificates

echo "==> Checking RAM / swap"
TOTAL_RAM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
TOTAL_RAM_MB=$((TOTAL_RAM_KB / 1024))
if [ "$TOTAL_RAM_MB" -lt 2048 ] && ! sudo swapon --show | grep -q '/swapfile'; then
  echo "    RAM is ${TOTAL_RAM_MB}MB (<2GB) and no /swapfile active — adding a 2GB swap file."
  if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
  fi
  sudo swapon /swapfile
  if ! grep -q '^/swapfile ' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  fi
else
  echo "    RAM is ${TOTAL_RAM_MB}MB or swap is already active — skipping."
fi

echo "==> Installing Node.js 22 (if missing)"
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | sed 's/^v//' | cut -d. -f1)" -lt 22 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
else
  echo "    Node $(node -v) already installed — skipping."
fi

echo "==> Installing Claude Code CLI (if missing)"
if ! command -v claude >/dev/null 2>&1; then
  sudo npm install -g @anthropic-ai/claude-code
else
  echo "    claude CLI already installed ($(claude --version 2>/dev/null || echo unknown version)) — skipping."
fi

echo "==> Pre-seeding Claude CLI project trust (defensive; SDK/headless runs don't need this, but costs nothing)"
python3 - "$APP_DIR" <<'EOF'
import json
import os
import sys

app_dir = sys.argv[1]
config_file = os.path.expanduser("~/.claude.json")
config = {}
if os.path.exists(config_file):
    with open(config_file) as f:
        config = json.load(f)

config.setdefault("projects", {})
config["projects"].setdefault(app_dir, {})
config["projects"][app_dir]["hasTrustDialogAccepted"] = True

with open(config_file, "w") as f:
    json.dump(config, f, indent=2)
os.chmod(config_file, 0o600)
EOF

echo "==> Smoke-testing the claude CLI headlessly"
if [ -f "$APP_DIR/.env" ] && grep -q '^ANTHROPIC_API_KEY=' "$APP_DIR/.env" && ! grep -q '^ANTHROPIC_API_KEY=your_anthropic_api_key_here' "$APP_DIR/.env"; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_DIR/.env"
  set +a
  if claude -p "reply with the word ok" --model "${CLAUDE_MODEL:-claude-sonnet-5}" >/tmp/claude_smoke_test.log 2>&1; then
    echo "    OK — claude CLI runs headlessly with the configured API key."
  else
    echo "    WARNING: headless claude CLI test failed — see /tmp/claude_smoke_test.log" >&2
  fi
else
  echo "    Skipping (ANTHROPIC_API_KEY not yet set in .env) — re-run setup.sh after filling in .env to test this."
fi

echo "==> Creating virtualenv"
if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi

echo "==> Installing Python requirements"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Installing systemd services"
sudo cp "$APP_DIR/deploy/sales-intellect-flask.service" /etc/systemd/system/
sudo cp "$APP_DIR/deploy/sales-intellect-streamlit.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sales-intellect-flask
sudo systemctl enable --now sales-intellect-streamlit

echo
echo "==> Done."
if [ ! -f "$APP_DIR/.env" ]; then
  echo "No .env found — copy .env.example to .env and fill in real values, then:"
  echo "  sudo systemctl restart sales-intellect-flask sales-intellect-streamlit"
fi
echo "Check status with:"
echo "  sudo systemctl status sales-intellect-flask sales-intellect-streamlit"
echo "  sudo journalctl -u sales-intellect-flask -u sales-intellect-streamlit -f"
