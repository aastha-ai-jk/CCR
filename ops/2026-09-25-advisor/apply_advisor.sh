#!/bin/bash
# Deploy the 2026-09-25 CCR advisor + operator-decision log to the GPU box.
#
# Safe by construction:
#  1. pre-flight: every target file must be byte-identical to the version this
#     bundle was built against (MANIFEST.tsv) — otherwise NOTHING is touched,
#     so server-only edits are never overwritten;
#  2. every replaced file is backed up as <file>.bak-<timestamp>;
#  3. any failure after install (compile, import, build, health check) rolls
#     back automatically via rollback_advisor.sh.
# Restarts ONLY ccr-backend and ccr-frontend. The live feed, shadow, optimizer,
# monitor and retrain processes and the database are not touched.
set -euo pipefail
APP=/home/antpc/CCR-Project
HERE="$(cd "$(dirname "$0")" && pwd)"
TS=$(date +%Y%m%d-%H%M%S)
cd "$APP"

echo "=== 1/6 pre-flight: server files vs bundle base ==="
bad=0
while IFS=$'\t' read -r path base new; do
  have=$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1 || true)
  if [ "$have" = "$new" ]; then
    echo "  already deployed : $path"
  elif [ "$base" = "NEW" ] && [ -z "$have" ]; then
    echo "  new file         : $path"
  elif [ "$have" = "$base" ]; then
    echo "  ok (base)        : $path"
  else
    echo "  MISMATCH         : $path  (server file differs from the bundle's base)"
    bad=1
  fi
done < "$HERE/MANIFEST.tsv"
if [ "$bad" -ne 0 ]; then
  echo "ABORT: nothing was changed. The MISMATCH files were edited on the server;"
  echo "       copy them back to the repo so the change can be merged, then rebuild the bundle."
  exit 1
fi
if [ "${1:-}" = "--check" ]; then
  echo "CHECK ONLY: pre-flight passed, nothing was changed."
  exit 0
fi
[ -x .venv/bin/python ] || { echo "ABORT: $APP/.venv/bin/python not found"; exit 1; }
command -v pm2 >/dev/null || { echo "ABORT: pm2 not on PATH"; exit 1; }

echo "=== 2/6 backup + install (backup suffix .bak-$TS) ==="
echo "$TS" > "$HERE/LAST_BACKUP"
trap 'echo "!!! step failed — rolling back to the .bak-$TS files"; bash "$HERE/rollback_advisor.sh" "$TS"; exit 1' ERR
while IFS=$'\t' read -r path base new; do
  if [ -e "$path" ]; then cp -p "$path" "$path.bak-$TS"; fi
  mkdir -p "$(dirname "$path")"
  cp "$HERE/payload/$path" "$path"
  echo "  installed $path"
done < "$HERE/MANIFEST.tsv"

echo "=== 3/6 compile + import smoke test ==="
PYS=$(cut -f1 "$HERE/MANIFEST.tsv" | grep '\.py$' | tr '\n' ' ')
.venv/bin/python -m py_compile $PYS
find backend twin etl -name "*.pyc" -delete 2>/dev/null || true
.venv/bin/python -c "import config, json; json.load(open('data/plant_settings.json')); json.load(open('data/validation_limits.json')); import backend.analysis, backend.decisions, backend._fuel_dashboard, backend.guardrails, backend.validation, backend.main, twin.features, twin.model, twin.economics, twin.optimize, twin.reasoning, twin.shadow, twin.optimizer_loop, twin.monitor, twin.retrain, twin.train_gbt, etl.clean, etl.load, etl.live_feed, etl.lab_live, etl.freelime_feed, etl.hotmeal_feed; print('  IMPORT_OK')"

echo "=== 4/6 frontend production build ==="
(cd frontend && npm run build 2>&1 | tail -15)   # pipefail: a failed build trips the ERR trap

echo "=== 5/6 restart ccr-backend + ccr-frontend only ==="
pm2 restart ccr-backend ccr-frontend --update-env >/dev/null
sleep 10

echo "=== 6/6 health checks ==="
for u in /api/health /api/decisions /api/decisions/days /api/advisor/summary /api/admin/declined-reached /api/admin/accuracy /api/validation /advisor; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "http://localhost:8010$u")
  echo "  backend $u -> $code"
  [ "$code" = "200" ]
done
for u in /twin /admin; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "http://localhost:3010$u")
  echo "  frontend $u -> $code"
  [ "$code" = "200" ]
done
trap - ERR
echo "  twin forecast (first call loads the model, can take a minute):"
curl -s --max-time 240 http://localhost:8010/api/twin/forecast | head -c 400 || true
echo
echo "DONE. Advisor: http://10.53.2.10:8010/advisor   Twin: http://10.53.2.10:3010/twin"
echo "Rollback if needed: bash $HERE/rollback_advisor.sh $TS"
