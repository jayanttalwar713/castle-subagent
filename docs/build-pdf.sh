#!/usr/bin/env bash
# Render docs/design.html -> DESIGN.pdf using headless Chrome.
# DESIGN.md is the canonical doc; design.html is the printable typeset version.
set -euo pipefail

cd "$(dirname "$0")/.."

CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[ -x "$CHROME" ] || { echo "Chrome not found at: $CHROME (override with \$CHROME)" >&2; exit 1; }

"$CHROME" \
  --headless \
  --disable-gpu \
  --no-pdf-header-footer \
  --virtual-time-budget=10000 \
  --print-to-pdf="$PWD/DESIGN.pdf" \
  "file://$PWD/docs/design.html"

echo "wrote DESIGN.pdf"
