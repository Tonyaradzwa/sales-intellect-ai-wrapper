#!/usr/bin/env bash
# Starts the Flask backend (server.py) and the Streamlit UI (app.py) together.
# Ctrl+C stops both.
set -e

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

python3 server.py &
BACKEND_PID=$!
trap "kill $BACKEND_PID" EXIT

streamlit run app.py
