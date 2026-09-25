#!/bin/bash
# Undo apply_advisor.sh: restore the .bak-<timestamp> copies, remove files the
# bundle added, rebuild the frontend and restart ccr-backend + ccr-frontend.
# The operator_decisions table (and any decisions in it) is left in place.
#   bash rollback_advisor.sh [timestamp]   (default: the last apply run)
set -uo pipefail
APP=/home/antpc/CCR-Project
HERE="$(cd "$(dirname "$0")" && pwd)"
TS="${1:-$(cat "$HERE/LAST_BACKUP" 2>/dev/null || true)}"
[ -n "$TS" ] || { echo "no backup timestamp given or recorded"; exit 1; }
cd "$APP"

while IFS=$'\t' read -r path base new; do
  if [ -e "$path.bak-$TS" ]; then
    cp -p "$path.bak-$TS" "$path" && echo "  restored $path"
  elif [ "$base" = "NEW" ]; then
    rm -f "$path" && echo "  removed  $path"
  fi
done < "$HERE/MANIFEST.tsv"
find backend twin -name "*.pyc" -delete
(cd frontend && npm run build 2>&1 | tail -5)
pm2 restart ccr-backend ccr-frontend --update-env >/dev/null && echo "ROLLED BACK to pre-$TS"
