#!/bin/bash
# Bring the ATM Site Map demo up on a fresh quick tunnel.
#   ./start-demo.sh            keep the current sites.db
#   ./start-demo.sh --reseed   reset to the pristine demo seed
set -u
cd "$(dirname "$0")" || exit 1
USER="${DEMO_USER:-demo}"
: "${DEMO_PASS:?Set DEMO_PASS to a strong, unique password before starting a public tunnel}"
PW="$DEMO_PASS"

echo "── stopping anything already running ──"
lsof -ti :8093 2>/dev/null | xargs kill -9 2>/dev/null
pkill -f "cloudflared tunnel --url" 2>/dev/null
sleep 2

if [ "${1:-}" = "--reseed" ]; then
  cp demo_seed.db sites.db && rm -f sites.db-shm sites.db-wal
  echo "   sites.db reset from demo_seed.db"
fi
rm -f .killswitch

echo "── starting app ──"
DEMO_USER="$USER" DEMO_PASS="$PW" HARD_CALL_CAP=1000 \
  nohup python3 proxy.py > proxy.log 2>&1 &
sleep 4
if ! curl -s -o /dev/null -u "$USER:$PW" http://127.0.0.1:8093/ ; then
  echo "   !! app failed to start — check proxy.log"; tail -5 proxy.log; exit 1
fi
echo "   app up on 127.0.0.1:8093 (auth on)"

echo "── opening tunnel ──"
nohup cloudflared tunnel --url http://127.0.0.1:8093 > cf.log 2>&1 &
URL=""
for i in $(seq 1 45); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" cf.log 2>/dev/null | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
[ -z "$URL" ] && { echo "   !! tunnel failed — check cf.log"; exit 1; }

echo "── keeping the laptop awake ──"
pgrep -f "caffeinate -dis" >/dev/null || (nohup caffeinate -dis >/dev/null 2>&1 &)

HOST=${URL#https://}
cat <<INFO

  ════════════════════════════════════════════════════════════
   URL   $URL
   user  $USER
   pass  $PW
  ════════════════════════════════════════════════════════════

  BEFORE SHARING — add this to MAPS_BROWSER_KEY's website
  restrictions in the Google Cloud console, or the Google map
  layers silently fail and only "Satellite (Esri)" appears:

     $HOST/*

  Watch traffic:  tail -f visits.log
  Stop it all:    ./stop-demo.sh
  Emergency stop: touch .killswitch   (halts all Google API spend)

INFO
