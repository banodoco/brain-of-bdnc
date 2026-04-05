#!/bin/bash
# Run the monthly equity review pipeline
#
# Usage:
#   ./scripts/run_equity_review.sh           # defaults to previous month
#   ./scripts/run_equity_review.sh 2026-03   # specific month
#
# Pipeline:
#   Layer 1 (code):   contributors command → signals JSON       (~30 seconds)
#   Layer 2 (LLM):    profile + evaluate → enriched signals JSON (~5-10 minutes)
#   Layer 3 (human):  POM reviews the enriched signals file

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Determine month
if [ -n "${1:-}" ]; then
    MONTH="$1"
else
    if [ "$(uname)" = "Darwin" ]; then
        MONTH=$(date -v-1m +%Y-%m)
    else
        MONTH=$(date -d "last month" +%Y-%m)
    fi
fi

SIGNALS_FILE="$PROJECT_DIR/results/${MONTH}_signals.json"

echo "=== Equity Review Pipeline for $MONTH ==="
echo ""

# Layer 1: Generate signals
echo "--- Layer 1: Generating contributor signals ---"
cd "$PROJECT_DIR"
python scripts/discord_tools.py contributors --month "$MONTH" --min-signals 2 --output "$SIGNALS_FILE"
echo "Signals written to: $SIGNALS_FILE"
echo ""

# Layer 2: LLM evaluation
echo "--- Layer 2: Running qualitative evaluation ---"
PROMPT=$(sed "s/{{MONTH}}/$MONTH/g" "$SCRIPT_DIR/equity_review_layer2_prompt.md")
claude -p "$PROMPT" --allowedTools "Bash,Read,Write"
echo ""

echo "=== Done ==="
echo "Output: $SIGNALS_FILE (now enriched with Layer 2 evaluations)"
echo ""
echo "Layer 3: Open the file and review. Filter by verdict: strong, moderate, weak, notable_mention."
