#!/usr/bin/env bash
# Quick start for local dev / bare-metal deployment (no Docker)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$SCRIPT_DIR/backend"

cd "$BACKEND"

# Create venv if missing
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi

source .venv/bin/activate

# Install / upgrade deps
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Ensure .env exists
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "⚠️  Created backend/.env from .env.example"
  echo "    Edit it and fill in your B2 credentials before running again."
  echo ""
  exit 1
fi

# Create data directory for SQLite
mkdir -p "$SCRIPT_DIR/data"
export DATABASE_URL="sqlite:///$SCRIPT_DIR/data/datadock.db"

echo "Starting DataDock on http://0.0.0.0:8000 …"
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2 --reload
