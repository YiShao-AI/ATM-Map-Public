# Protected-demo deployment runbook

This runbook covers a single-host deployment of the ATM Map behind HTTPS and
password protection. The application binds to localhost; the tunnel is the only
public ingress path.

```text
reviewer browser ──HTTPS tunnel──► basic authentication
                                         │
                                         ▼
                              127.0.0.1:8093 application
                                  │              │
                               SQLite       metered APIs
```

## Deployment assets

| Path | Purpose | Repository policy |
|---|---|---|
| `demo_seed.db` | reset point for the demonstration workflow | committed demonstration data |
| `sites.db` | active demonstration state | committed initial snapshot; changes are runtime state |
| `atms.js` | 33 historical decommissioned machines with serial numbers and addresses | committed historical layer |
| `.cache-photos/` | cached demonstration photos | committed to avoid repeat paid requests |
| `.cache/` | expiring search response cache | ignored |
| `.usage.json` | daily and lifetime API ledger | ignored |
| `.env` | server and browser API keys | ignored; never commit |
| `proxy.log`, `visits.log`, tunnel logs | runtime and access records | ignored |

## 1. Prepare the host

Requirements:

- Python 3.9 or newer;
- `curl` for smoke checks;
- Cloudflare Tunnel or Tailscale Funnel for HTTPS; and
- restricted Google Places/Geocoding and browser map keys for live search.

```bash
git clone https://github.com/YiShao-AI/ATM-Map-Public.git
cd ATM-Map-Public
cp .env.example .env
```

Fill in `.env` locally. Use separate keys:

- `GOOGLE_MAPS_API_KEY` is server-side and restricted to the required APIs and
  host IP where practical.
- `MAPS_BROWSER_KEY` is intentionally delivered to the browser and must be
  restricted by the HTTPS demo hostname.

## 2. Verify locally

```bash
python3 -m unittest discover -s tests -v
python3 proxy.py
```

Open <http://127.0.0.1:8093>. Confirm that the map, existing-location layer,
pipeline layer, Saved view, Revisit view, Search History, and usage status load before exposing
the service.

## 3. Enable authentication

Authentication is enabled only when both values are present:

```bash
export DEMO_USER=demo
export DEMO_PASS='<strong unique password>'
HARD_CALL_CAP=1000 python3 proxy.py
```

Verify the boundary:

```bash
curl -s http://127.0.0.1:8093/healthz
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8093/
curl -s -o /dev/null -w '%{http_code}\n' \
  --user "${DEMO_USER}:${DEMO_PASS}" http://127.0.0.1:8093/
```

Expected results are `{"ok": true}` for the intentionally public, data-free
health probe, `401` for the application without credentials, and `200` with
credentials.

## 4. Expose through HTTPS

For a temporary review session:

```bash
cloudflared tunnel --url http://127.0.0.1:8093
```

For a stable host without a separate domain:

```bash
tailscale funnel 8093
```

For a stable owned domain, use a named Cloudflare Tunnel and route its hostname
to the localhost service. In every case, retain application authentication or
place the service behind an identity-aware access policy.

Add the final HTTPS hostname to the browser key’s referrer restrictions before
sharing it. A missing referrer rule produces a blank Google basemap even when
the server-side search key is working.

Point an external uptime monitor at `https://YOUR-HOST/healthz`. Keep that route
data-free: it deliberately bypasses application authentication so monitoring
does not require the reviewer password.

## 5. Enforce spending controls

The service provides layered controls rather than relying on billing alerts
alone:

1. search is initiated explicitly by the user;
2. the UI previews expected API calls and cost;
3. cached coverage is reused;
4. each search has a request budget;
5. burst and daily limits reject excess requests;
6. `HARD_CALL_CAP` bounds lifetime calls for the deployment; and
7. `.killswitch` immediately blocks new paid requests.

The configured search price reflects the highest tier in the response field
mask: Text Search Enterprise (`$35 / 1,000`, 1,000 free events per month,
checked 2026-09-03). Free usage is shared at the billing-account level, so the
local ledger is a planning estimate; reconcile it against Google Cloud Billing
before raising a deployment cap.

Check current state:

```bash
curl -s --user "${DEMO_USER}:${DEMO_PASS}" \
  https://YOUR-HOST/api/status | python3 -m json.tool
```

Emergency stop:

```bash
touch .killswitch
```

Removing the file clears the manual stop but does not reset a reached lifetime
cap.

## 6. Reset or back up demonstration state

Reset only when no reviewer session is active:

```bash
cp demo_seed.db sites.db
```

The application also supports a consistent SQLite snapshot through
`store.backup()`, which uses `VACUUM INTO` so a running service does not produce
an inconsistent file copy.

## 7. Final smoke test

- [ ] unauthenticated request returns `401`;
- [ ] authenticated request returns `200`;
- [ ] unauthenticated `/healthz` returns exactly `{"ok": true}` and no
  operational data;
- [ ] browser map tiles render over HTTPS;
- [ ] 33 decommissioned ATM locations appear with serial numbers and addresses;
- [ ] pipeline records appear in their own layer;
- [ ] candidate search requires an explicit action and updates the usage display;
- [ ] a cached repeat does not repeat paid upstream calls;
- [ ] Search History can reopen a completed run by ID and an interrupted test
  run retains its last checkpoint;
- [ ] a simulated provider failure after one completed category displays the
  checkpointed results and identifies the unfinished categories;
- [ ] Verify-first can be manually moved to Ready to mail and reversed;
- [ ] Revisit and Saved views load;
- [ ] source files, databases, environment files, and traversal paths return
  `403`; and
- [ ] `/api/status` reports the configured lifetime cap and remaining calls.

## 8. Shutdown and retention

Stop the tunnel first, then the local service. Retain the SQLite backup required
for the review period, and remove runtime access logs according to the agreed
retention window. Rotate shared demo credentials after the review closes.
