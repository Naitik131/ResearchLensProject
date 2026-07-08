#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# ResearchLens Launcher
# Usage: ./run.sh
# ─────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║         ResearchLens — AI Literature Review      ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Check .env
if [ ! -f .env ]; then
  echo "⚠️  No .env file found. Creating one..."
  echo "GOOGLE_API_KEY=your_gemini_api_key_here" > .env
  echo "   → Edit .env and add your GOOGLE_API_KEY, then re-run."
  exit 1
fi

# Install dependencies if needed
if ! python3 -c "import flask" 2>/dev/null; then
  echo "📦 Installing dependencies..."
  pip install -r requirements_gui.txt --quiet
fi

echo "🚀 Starting server at http://localhost:5000"
echo "   Press Ctrl+C to stop."
echo ""
python3 app.py
