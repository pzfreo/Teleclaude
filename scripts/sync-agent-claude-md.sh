#!/usr/bin/env bash
# Sync the user's personal ~/.claude/CLAUDE.md into agent/CLAUDE.md.
# Run this before committing if your local guidelines have changed.
set -euo pipefail

SRC="${HOME}/.claude/CLAUDE.md"
DST="$(cd "$(dirname "$0")/.." && pwd)/agent/CLAUDE.md"

if [[ ! -f "$SRC" ]]; then
    echo "error: $SRC does not exist" >&2
    exit 1
fi

cp "$SRC" "$DST"
echo "Synced $SRC → $DST"
echo "Review with: git diff agent/CLAUDE.md"
