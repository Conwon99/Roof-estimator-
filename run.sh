#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "Installing dependencies..."
  pip install -r requirements.txt -q
fi

echo "Starting Roof Size Estimator on http://localhost:8000"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
