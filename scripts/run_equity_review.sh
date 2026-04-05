#!/bin/bash
# Run the monthly equity review using Claude Code
#
# Usage:
#   ./scripts/run_equity_review.sh           # defaults to previous month
#   ./scripts/run_equity_review.sh 2025-08   # specific month

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Determine month
if [ -n "${1:-}" ]; then
    MONTH="$1"
else
    # Default to previous full month
    if [ "$(uname)" = "Darwin" ]; then
        MONTH=$(date -v-1m +%Y-%m)
    else
        MONTH=$(date -d "last month" +%Y-%m)
    fi
fi

echo "Running equity review for: $MONTH"

# Build the prompt from the template
PROMPT=$(sed "s/{{MONTH}}/$MONTH/g" "$SCRIPT_DIR/equity_review_prompt.md")

# Run Claude Code with the prompt
cd "$PROJECT_DIR"
claude -p "$PROMPT"
