#!/bin/bash
# PWC Crime Dashboard — Local Startup Script
# Double-click this file (or run it in Terminal) to start the dashboard.

set -e
cd "$(dirname "$0")"

echo "================================================"
echo "  PWC Crime Dashboard — Starting up..."
echo "================================================"

# Install dependencies (only needed first time)
pip install -r requirements.txt --break-system-packages -q

echo ""
echo "✓ Dependencies ready"
echo "✓ Opening dashboard at http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop the server."
echo "================================================"
echo ""

# Open browser after a short delay
(sleep 2 && open http://localhost:8000) &

# Start the server
python main.py
