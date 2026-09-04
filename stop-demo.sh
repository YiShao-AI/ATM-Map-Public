#!/bin/bash
# Tear the demo down completely.
cd "$(dirname "$0")" || exit 1
lsof -ti :8093 2>/dev/null | xargs kill -9 2>/dev/null && echo "app stopped"
pkill -f "cloudflared tunnel --url" 2>/dev/null && echo "tunnel closed"
pkill -f "caffeinate -dis" 2>/dev/null && echo "sleep re-enabled"
sleep 1
lsof -i :8093 >/dev/null 2>&1 && echo "!! port 8093 still bound" || echo "port 8093 clear"
echo "visits.log kept."
