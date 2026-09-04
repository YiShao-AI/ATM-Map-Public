#!/usr/bin/env python3
"""
ATM Site Map — backend proxy.

Holds the Google API key server-side (never sent to the browser), proxies Places
searches, normalizes results into the shape the front end consumes, protects
spend with a multi-layer killswitch, and persists site status / outreach records.

Also serves the static app, so there is one origin and no CORS.

Run:   cd ~/atm-map && python3 proxy.py           # http://127.0.0.1:8093
Env:   GOOGLE_MAPS_API_KEY  (read from ./.env; never logged, never served)

── Spend protection ────────────────────────────────────────────────────────
Google Cloud budgets ALERT rather than stop — they email while charges continue —
so this proxy is the only ceiling that actually halts a call. Five layers, all
checked before any upstream request:
  1. Manual kill      — .killswitch file (survives restart; `touch .killswitch`)
  2. Hard lifetime cap— HARD_CALL_CAP, cumulative and never reset (for exposed
                        deployments; a day rollover does not restore capacity)
  3. Daily call cap   — DAILY_CALL_CAP
  4. Burst guard      — BURST_MAX calls in BURST_WINDOW_S (catches runaway loops)
  5. Spend estimate   — MONTHLY_BUDGET_USD, from published per-SKU rates
Plus a disk cache: a repeated area+segment costs 0 upstream calls.
"""
from __future__ import annotations
import base64, hmac, json, os, re, hashlib, time, math, threading
import urllib.request, urllib.parse, urllib.error
from collections import deque
from datetime import date, datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Optional CRM seam. The app works fully without it; see crm_adapter.py.
import store

try:
    import crm_adapter as crm
except Exception:
    crm = None
CACHE_DIR  = ROOT / ".cache"
USAGE_FILE = ROOT / ".usage.json"
SITES_FILE = ROOT / "sites.json"
KILL_FILE  = ROOT / ".killswitch"
PORT = int(os.getenv("PORT", "8093"))
APP_VERSION = os.getenv("APP_VERSION", "")
# Append-only visitor log: who hit what, when, from where. Tab-separated.
VISIT_LOG = os.getenv("VISIT_LOG", str(ROOT / "visits.log"))

# ── optional access control ────────────────────────────────────────────────
# Off by default: a local tool on 127.0.0.1 needs no password. Set both to
# require HTTP Basic auth on every request, which is what makes it safe to put
# this behind a public tunnel. Compared in constant time so a wrong password
# cannot be recovered by timing the response.
DEMO_USER = os.getenv("DEMO_USER", "")
DEMO_PASS = os.getenv("DEMO_PASS", "")
AUTH_ON   = bool(DEMO_USER and DEMO_PASS)


def _auth_ok(header: str | None) -> bool:
    if not AUTH_ON:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    # Both compared regardless of the first result, so failure timing is flat.
    return hmac.compare_digest(user, DEMO_USER) & hmac.compare_digest(pw, DEMO_PASS)

# ── spend guards ───────────────────────────────────────────────────────────
# HARD_CALL_CAP is a cumulative ceiling for the life of this deployment. Unlike
# the daily cap it NEVER resets, so an exposed instance cannot be drained by
# coming back tomorrow. On reaching it the killswitch trips permanently: clearing
# the kill file does not restore capacity, because this check re-trips on the
# next call. Raising the limit is therefore a deliberate act, not a wait.
HARD_CALL_CAP       = int(os.getenv("HARD_CALL_CAP", "1000"))
# The Text Search free-usage cap is shared at the billing-account level, while
# this service can only observe its own ledger. The local estimate below is
# therefore a planning aid; Google Cloud Billing remains the invoice authority.
# Burst must exceed MAX_CALLS_PER_SEARCH (40) or a single legitimate tiled
# search trips the guard — it is there to catch runaway loops, not normal tiling.
DAILY_CALL_CAP      = int(os.getenv("DAILY_CALL_CAP", "250"))
BURST_MAX           = int(os.getenv("BURST_MAX", "80"))         # calls per window
BURST_WINDOW_S      = int(os.getenv("BURST_WINDOW_S", "60"))
MONTHLY_BUDGET_USD  = float(os.getenv("MONTHLY_BUDGET_USD", "5.00"))
CACHE_TTL_S         = int(os.getenv("CACHE_TTL_S", str(7 * 24 * 3600)))

# Published global rates (USD per 1,000) and monthly free-usage caps. Google
# bills a field-mask request at the highest tier requested. FIELD_MASK includes
# hours, phone, rating, rating count, and website, so search is Enterprise—not
# Pro. Re-verify periodically; these values drive the local estimate and guard.
PRICING_AS_OF = "2026-09-03"
PRICING_SOURCE = "https://developers.google.com/maps/billing-and-pricing/pricing"
SKU = {
    "search":  {"per_1k": 35.00, "free_month": 1000,  "label": "Places Text Search (Enterprise)"},
    "geocode": {"per_1k":  5.00, "free_month": 10000, "label": "Geocoding (Essentials)"},
    "photo":   {"per_1k":  7.00, "free_month": 1000,  "label": "Place Photos"},
}

FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.rating", "places.userRatingCount", "places.businessStatus",
    "places.regularOpeningHours", "places.nationalPhoneNumber",
    "places.websiteUri", "places.primaryType", "places.types", "places.photos",
    "nextPageToken",
])

# ── does a site have an indoor shop, and is an ATM already there? ──────────
# An ATM lives inside retail space, so a pumps-only forecourt is not a viable
# host. Google's `types` array helps but UNDER-reports: measured 2026-08-29,
# only 40% of sampled gas stations carried a shop type (real-world rate is far
# higher) and 7% carried an `atm` type. So presence is trustworthy, absence is
# not — these resolve to "yes" / "likely" / "unknown", never "no".
_SHOP_TYPES = {"convenience_store", "food_store", "grocery_store",
               "supermarket", "store", "department_store"}
# Fuel brands whose forecourts almost always include a c-store.
_CSTORE_BRANDS = re.compile(
    r"\b(ampm|am/pm|extramile|extra mile|on the run|circle k|7[- ]?eleven|"
    r"quiktrip|quik trip|wawa|sheetz|racetrac|speedway|casey|kum & go|maverik|"
    r"cumberland|royal farms|kwik trip|getgo|thorntons|buc-?ee|pilot|flying j|"
    r"love'?s|corner store|food mart|mini mart|market)\b", re.I)


# ── is a COMPETITOR machine already on site? ───────────────────────────────
# Google's `atm` type is generic: it means a cash machine, with no distinction
# between a bank ATM and a Bitcoin one. Those are opposite signals for us. A bank
# ATM is not a blocker and is arguably positive -- the site already hosts a
# machine, so it has the space, the power, and an owner who has agreed to one
# before. The real blocker is a competitor BTC machine, which Google does not tag
# at all; it shows up in the host's name or nowhere. Verified in live data: five
# sites carried the `atm` type (76, ampm, a liquor store -- all bank machines),
# while "AIRPORT MARKET WE HAVE BITCOIN AND MANY THINGS" carried none.
_BTC_OPERATORS = re.compile(
    r"\b(bitcoin|bitcoin ?depot|coinflip|coin ?flip|coinhub|coin ?hub|athena|"
    r"rockitcoin|rockit ?coin|libertyx|liberty ?x|bitstop|coinme|byte ?federal|"
    r"coin ?cloud|cryptobase|crypto|\bbtc\b)", re.I)


def btc_competitor(name: str, extra: str = "") -> str:
    """'likely' | 'unknown' -- never 'no'. A name is weak evidence of presence and
    no evidence at all of absence, so this can only ever raise a flag to check."""
    return "likely" if _BTC_OPERATORS.search(f"{name} {extra}") else "unknown"


def shop_presence(place: dict, name: str) -> str:
    """'yes' | 'likely' | 'unknown' — never 'no' (the data cannot prove absence)."""
    types = set(place.get("types") or [])
    primary = place.get("primaryType") or ""
    if types & _SHOP_TYPES or primary in _SHOP_TYPES:
        return "yes"
    if _CSTORE_BRANDS.search(name or ""):
        return "likely"
    return "unknown"

# ── adaptive coverage ──────────────────────────────────────────────────────
# Places Text Search returns 20 per page and at most 3 pages (hard ceiling 60
# unique results per circle, verified empirically 2026-08-29). Strategy:
#   1. Paginate first  — 3 calls buys 60 results from ONE circle. Cheaper than
#      splitting, which costs 4+ calls for a smaller area each.
#   2. Only a full 60 proves the circle actually overflows; then quadtree-split
#      it into 4 children and recurse on those (and only those).
#   3. Sparse circles stop after 1 call and never split — so cost tracks real
#      density, not area.
# Tiles are RECTANGLES sent as locationRestriction, not circles as locationBias.
# Verified 2026-08-29: locationBias only *biases* — a 1000 m biased search
# returned places up to 3.1 km away, so children kept re-returning the parent's
# results and subdivision bought almost nothing. locationRestriction+rectangle
# genuinely restricts (0 of 13 results fell outside the box), and rectangles
# quadrant-split with no overlap and no gaps.
PAGE_SIZE            = 20
MAX_PAGES            = 3            # API ceiling
PAGE_CEILING         = PAGE_SIZE * MAX_PAGES        # 60
MAX_DEPTH            = int(os.getenv("MAX_DEPTH", "2"))
MIN_TILE_M           = float(os.getenv("MIN_TILE_M", "250"))   # stop splitting below this
MAX_CALLS_PER_SEARCH = int(os.getenv("MAX_CALLS_PER_SEARCH", "40"))

# Ceiling on a single search radius. This is a sanity bound, not a cost control:
# spend is capped by MAX_CALLS_PER_SEARCH no matter how large the area is, so a
# wide radius costs no more than a narrow one -- it just covers less of itself.
# The UI reads this from /api/config so the circle drawn on the map can never
# claim more ground than the search actually covers.
MAX_RADIUS_M = float(os.getenv("MAX_RADIUS_M", "804672"))   # 500 miles
SEARCH_URL  = "https://places.googleapis.com/v1/places:searchText"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

SEGMENT_QUERIES = {
    "smoke":       "smoke shop vape shop tobacco",
    "liquor":      "liquor store wine spirits",
    "gas":         "gas station",
    "laundromat":  "laundromat laundry",
    "convenience": "convenience store mini mart",
}
_TYPE_SEG = {
    "liquor_store": "liquor", "gas_station": "gas", "laundry": "laundromat",
    "convenience_store": "convenience", "tobacco_shop": "smoke",
    "supermarket": "convenience", "grocery_store": "convenience",
}
_NAME_SEG = [
    ("smoke",       re.compile(r"\b(smoke|vape|tobacco|cigar|hookah)\b", re.I)),
    ("liquor",      re.compile(r"\b(liquor|wine|spirits|bottle shop)\b", re.I)),
    ("gas",         re.compile(r"\b(arco|chevron|shell|mobil|exxon|valero|gas|fuel|station)\b", re.I)),
    ("laundromat",  re.compile(r"\b(laundr|wash)\b", re.I)),
    ("convenience", re.compile(r"\b(market|mart|convenience|grocery|deli)\b", re.I)),
]

_lock = threading.Lock()
_burst: deque[float] = deque()


# ── env / key ──────────────────────────────────────────────────────────────
def load_env() -> None:
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _key() -> str:
    k = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not k:
        raise RuntimeError("GOOGLE_MAPS_API_KEY not set (see .env)")
    return k


# ── usage + spend ──────────────────────────────────────────────────────────
def _blank_usage() -> dict:
    return {"date": date.today().isoformat(), "month": date.today().strftime("%Y-%m"),
            "calls": 0, "cache_hits": 0, "by_sku": {k: 0 for k in SKU},
            "month_by_sku": {k: 0 for k in SKU}}


def _usage() -> dict:
    try:
        u = json.loads(USAGE_FILE.read_text())
    except Exception:
        u = _blank_usage()
    today, month = date.today().isoformat(), date.today().strftime("%Y-%m")
    if u.get("date") != today:                      # new day: reset daily counters
        u.update({"date": today, "calls": 0, "cache_hits": 0,
                  "by_sku": {k: 0 for k in SKU}})
    if u.get("month") != month:                     # new month: free tier resets
        u.update({"month": month, "month_by_sku": {k: 0 for k in SKU}})
    u.setdefault("by_sku", {k: 0 for k in SKU})
    u.setdefault("month_by_sku", {k: 0 for k in SKU})
    u.setdefault("lifetime", 0)      # deliberately outside both resets above
    return u


def _save_usage(u: dict) -> None:
    try:
        USAGE_FILE.write_text(json.dumps(u))
    except Exception:
        pass


def estimate_spend(u: dict) -> float:
    """Month-to-date USD, counting only calls beyond each SKU's free allowance."""
    total = 0.0
    for sku, cfg in SKU.items():
        used = u.get("month_by_sku", {}).get(sku, 0)
        billable = max(0, used - cfg["free_month"])
        total += billable / 1000.0 * cfg["per_1k"]
    return round(total, 4)


def record_call(sku: str) -> dict:
    with _lock:
        u = _usage()
        u["calls"] = u.get("calls", 0) + 1
        u["lifetime"] = u.get("lifetime", 0) + 1
        u["by_sku"][sku] = u["by_sku"].get(sku, 0) + 1
        u["month_by_sku"][sku] = u["month_by_sku"].get(sku, 0) + 1
        _save_usage(u)
        _burst.append(time.time())
        return u


def record_cache_hit() -> None:
    with _lock:
        u = _usage()
        u["cache_hits"] = u.get("cache_hits", 0) + 1
        _save_usage(u)


# ── killswitch ─────────────────────────────────────────────────────────────
def kill_state() -> tuple[bool, str]:
    if KILL_FILE.exists():
        try:
            return True, KILL_FILE.read_text().strip() or "manual"
        except Exception:
            return True, "manual"
    return False, ""


def trip(reason: str) -> None:
    try:
        KILL_FILE.write_text(f"{reason} @ {datetime.now():%Y-%m-%d %H:%M:%S}")
    except Exception:
        pass


def clear_kill() -> None:
    try:
        KILL_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def guard() -> str | None:
    """Return a refusal reason, or None if an upstream call may proceed."""
    dead, why = kill_state()
    if dead:
        return f"killswitch active ({why})"
    u = _usage()
    if u.get("lifetime", 0) >= HARD_CALL_CAP:
        trip(f"HARD CAP: {HARD_CALL_CAP} lifetime calls reached")
        return f"hard cap reached ({HARD_CALL_CAP} total calls)"
    if u.get("calls", 0) >= DAILY_CALL_CAP:
        trip(f"auto: daily cap {DAILY_CALL_CAP} reached")
        return f"daily cap reached ({DAILY_CALL_CAP})"
    spend = estimate_spend(u)
    if spend >= MONTHLY_BUDGET_USD:
        trip(f"auto: estimated spend ${spend:.2f} >= budget ${MONTHLY_BUDGET_USD:.2f}")
        return f"monthly budget reached (est ${spend:.2f})"
    now = time.time()
    with _lock:
        while _burst and now - _burst[0] > BURST_WINDOW_S:
            _burst.popleft()
        if len(_burst) >= BURST_MAX:
            trip(f"auto: burst {len(_burst)} calls in {BURST_WINDOW_S}s")
            return f"burst guard ({BURST_MAX}/{BURST_WINDOW_S}s)"
    return None


def status() -> dict:
    u = _usage()
    dead, why = kill_state()
    spend = estimate_spend(u)
    return {
        "killed": dead, "kill_reason": why,
        "calls_today": u.get("calls", 0), "cap": DAILY_CALL_CAP,
        "lifetime_calls": u.get("lifetime", 0), "hard_cap": HARD_CALL_CAP,
        "remaining_lifetime": max(0, HARD_CALL_CAP - u.get("lifetime", 0)),
        "remaining_today": max(0, DAILY_CALL_CAP - u.get("calls", 0)),
        "cache_hits_today": u.get("cache_hits", 0),
        "by_sku_today": u.get("by_sku", {}), "month_by_sku": u.get("month_by_sku", {}),
        "est_spend_usd": spend, "budget_usd": MONTHLY_BUDGET_USD,
        "free_remaining": {k: max(0, SKU[k]["free_month"] - u.get("month_by_sku", {}).get(k, 0))
                           for k in SKU},
        "pricing": {"as_of": PRICING_AS_OF, "source": PRICING_SOURCE,
                    "usage_scope": "application-local estimate"},
        "month": u.get("month"),
    }


# ── cache ──────────────────────────────────────────────────────────────────
def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def cache_get(key: str):
    p = _cache_path(key)
    if not p.exists() or time.time() - p.stat().st_mtime > CACHE_TTL_S:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def prune_cache(max_files: int = 5000) -> int:
    """Delete expired cache entries. Nothing evicted them before, so the
    directory grew for the life of the install."""
    if not CACHE_DIR.exists():
        return 0
    now, removed, files = time.time(), 0, []
    for f in CACHE_DIR.glob("*.json"):
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if age > CACHE_TTL_S:
            try:
                f.unlink(); removed += 1
            except OSError:
                pass
        else:
            files.append(f)
    if len(files) > max_files:                    # hard ceiling, oldest first
        files.sort(key=lambda f: f.stat().st_mtime)
        for f in files[:len(files) - max_files]:
            try:
                f.unlink(); removed += 1
            except OSError:
                pass
    return removed


PHOTO_DIR = ROOT / ".cache-photos"


def _photo_path(ref: str, width: str) -> Path:
    h = hashlib.sha256(f"{ref}|{width}".encode()).hexdigest()[:32]
    return PHOTO_DIR / h


def photo_cache_get(ref: str, width: str):
    """Returns (bytes, content_type) or None. No TTL: a shop photo is static,
    and re-fetching it would spend from the smallest free allowance we have."""
    p = _photo_path(ref, width)
    if not p.exists():
        return None
    try:
        meta = p.with_suffix(".type")
        ctype = meta.read_text().strip() if meta.exists() else "image/jpeg"
        return p.read_bytes(), ctype
    except Exception:
        return None


def photo_cache_put(ref: str, width: str, data: bytes, ctype: str) -> None:
    try:
        PHOTO_DIR.mkdir(exist_ok=True)
        p = _photo_path(ref, width)
        p.write_bytes(data)
        p.with_suffix(".type").write_text(ctype)
    except Exception:
        pass


def cache_put(key: str, value) -> None:
    try:
        _cache_path(key).write_text(json.dumps(value))
    except Exception:
        pass


# ── site records (status / outreach / manual entries) ──────────────────────
def load_sites() -> dict:
    """Kept for compatibility — now backed by SQLite (see store.py)."""
    return store.all_sites()


# Retained for the manual-add endpoint; store.py owns the authoritative column
# list, so new fields only need adding there.
SITE_FIELDS = {"status", "reason", "reason_code", "owner", "last_contact",
               "next_action", "notes", "followup_date", "stage", "name", "address",
               "lat", "lng", "seg", "manual", "shortlist",
               "crm_id", "crm_synced_at"}
# status: prospect | contacted | rejected | do_not_contact | pipeline
STATUS_HIDE = {"rejected", "do_not_contact"}


def upsert_site(site_id: str, patch: dict, actor: str = "user") -> dict:
    """Delegates to the SQLite store, which owns field filtering, the monotonic
    funnel rule, decision history and the event log."""
    created = store.get(site_id) is None
    rec = store.upsert(site_id, patch, actor=actor)
    if crm:                       # fire-and-forget; a CRM outage never blocks a save
        try:
            crm.notify("site.created" if created else "site.updated", rec)
        except Exception:
            pass
    return rec


# ── upstream ───────────────────────────────────────────────────────────────
def _post_places(body: dict) -> dict:
    req = urllib.request.Request(
        SEARCH_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": _key(),
                 "X-Goog-FieldMask": FIELD_MASK})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def _rect_body(rect: dict) -> dict:
    return {"low":  {"latitude": rect["lo_lat"], "longitude": rect["lo_lng"]},
            "high": {"latitude": rect["hi_lat"], "longitude": rect["hi_lng"]}}


def resolve_place(item: dict, budget: dict):
    """Resolve an export-only record (name + address + coords, no place_id) to a
    Google place, in exactly one Text Search call.

    Returns (place_id, normalized_meta, confidence). Confidence is 'exact' when
    the returned address matches the export's normalized street, 'near' when it
    only falls inside the coordinate box, 'none' when nothing usable came back.
    The coordinate box is a `locationRestriction`, not a bias — bias was verified
    not to restrict at all, and an unrestricted name search matches the same
    brand in another town.
    """
    name = (item.get("name") or "").strip()
    addr = (item.get("address") or "").strip()
    lat, lng = item.get("lat"), item.get("lng")
    if not (name or addr) or lat is None or lng is None:
        return None, {}, "none"

    # Query with the street head only, not the full formatted address: verified
    # 2026-08-31 that appending city/state/zip returns 0 results where the street
    # alone matches exactly. 400 m, because exported coordinates sit tens of
    # metres off Google's own pin and a 200 m box misses on a correct address.
    street = addr.split(",")[0].strip()
    body = {"textQuery": " ".join(x for x in (name, street) if x), "pageSize": 5,
            "locationRestriction": {"rectangle": _rect_body(
                circle_to_rect(float(lat), float(lng), 400))}}
    try:
        data = _post_places(body)
    except Exception:
        return None, {}, "none"
    finally:
        budget["calls"] += 1
        record_call("search")

    want = _norm_addr(addr)
    places = data.get("places") or []

    # Fallback: the exported name can be stale (a shop changes hands) or worded
    # differently from Google's. The street address is the stable key, so retry on
    # it alone -- but accept ONLY an exact address match. Verified 2026-08-31 that
    # a loose street query otherwise returns a business on a different street, or
    # a neighbourhood rather than a business at all. The box is deliberately wide
    # here (5 km): some exported coordinates are over 3 km off even when not flagged
    # approximate, and requiring the exact address is what makes a wide search safe.
    if not places and street:
        try:
            data = _post_places({"textQuery": street, "pageSize": 5,
                                 "locationRestriction": {"rectangle": _rect_body(
                                     circle_to_rect(float(lat), float(lng), 5000))}})
        except Exception:
            return None, {}, "none"
        finally:
            budget["calls"] += 1
            record_call("search")
        for p in data.get("places") or []:
            rec = normalize(p, item.get("seg") or "")
            if want and _norm_addr(rec.get("address", "")) == want:
                return rec["place_id"], rec, "exact"
        return None, {}, "none"

    if not places:
        return None, {}, "none"

    for p in places:
        rec = normalize(p, item.get("seg") or "")
        if want and _norm_addr(rec.get("address", "")) == want:
            return rec["place_id"], rec, "exact"
    rec = normalize(places[0], item.get("seg") or "")
    return rec["place_id"], rec, "near"


def circle_to_rect(lat: float, lng: float, radius: float) -> dict:
    dlat = radius / 111320.0
    dlng = radius / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    return {"lo_lat": lat - dlat, "hi_lat": lat + dlat,
            "lo_lng": lng - dlng, "hi_lng": lng + dlng}


def rect_span_m(r: dict) -> float:
    """Shorter side of the rectangle, in metres."""
    mid = (r["lo_lat"] + r["hi_lat"]) / 2
    h = (r["hi_lat"] - r["lo_lat"]) * 111320.0
    w = (r["hi_lng"] - r["lo_lng"]) * 111320.0 * max(0.2, math.cos(math.radians(mid)))
    return min(h, w)


def _paginate(query: str, rect: dict, budget: dict) -> dict:
    """Fetch up to MAX_PAGES pages for one rectangle."""
    places, token, calls = [], None, 0
    for _ in range(MAX_PAGES):
        if budget["calls"] >= budget["max"]:
            return {"places": places, "calls": calls, "saturated": False,
                    "budget_hit": True}
        reason = guard()
        if reason:
            return {"places": places, "calls": calls, "saturated": False,
                    "refused": reason}
        body = {"textQuery": query,
                "locationRestriction": {"rectangle": _rect_body(rect)},
                "pageSize": PAGE_SIZE}
        if token:
            body["pageToken"] = token
        d = _post_places(body)
        record_call("search")
        calls += 1
        budget["calls"] += 1
        places.extend(d.get("places", []))
        token = d.get("nextPageToken")
        if not token:
            break
    return {"places": places, "calls": calls,
            "saturated": len(places) >= PAGE_CEILING}


def _children(r: dict):
    """Split a rectangle into 4 quadrants — no overlap, no gaps."""
    mlat = (r["lo_lat"] + r["hi_lat"]) / 2
    mlng = (r["lo_lng"] + r["hi_lng"]) / 2
    return [
        {"lo_lat": mlat, "hi_lat": r["hi_lat"], "lo_lng": r["lo_lng"], "hi_lng": mlng},
        {"lo_lat": mlat, "hi_lat": r["hi_lat"], "lo_lng": mlng, "hi_lng": r["hi_lng"]},
        {"lo_lat": r["lo_lat"], "hi_lat": mlat, "lo_lng": r["lo_lng"], "hi_lng": mlng},
        {"lo_lat": r["lo_lat"], "hi_lat": mlat, "lo_lng": mlng, "hi_lng": r["hi_lng"]},
    ]


def _adaptive(seg: str, rect: dict, budget: dict, depth: int = 0) -> dict:
    """Paginate a tile; subdivide only if it genuinely hit the API ceiling."""
    ck = ("v3|%s|%.4f,%.4f,%.4f,%.4f" % (seg, rect["lo_lat"], rect["lo_lng"],
                                         rect["hi_lat"], rect["hi_lng"]))
    cached = cache_get(ck)
    if cached is not None:
        record_cache_hit()
        page = {"places": cached["places"], "calls": 0,
                "saturated": cached.get("saturated", False)}
        cache_hits = 1
    else:
        page = _paginate(SEGMENT_QUERIES[seg], rect, budget)
        cache_hits = 0
    out = {"places": list(page["places"]), "calls": page["calls"],
           "cache_hits": cache_hits,
           "tiles": 1, "saturated": page.get("saturated", False), "max_depth": depth}
    for k in ("refused", "budget_hit"):
        if page.get(k):
            out[k] = page[k]
    if cached is None and not out.get("refused") and not out.get("budget_hit"):
        cache_put(ck, {"places": page["places"], "saturated": page.get("saturated")})
    # Split only when the tile actually hit the API ceiling (60), and only while
    # the children stay above the minimum useful size.
    if (page.get("saturated") and depth < MAX_DEPTH
            and rect_span_m(rect) / 2 >= MIN_TILE_M
            and not out.get("refused") and not out.get("budget_hit")):
        for child in _children(rect):
            sub = _adaptive(seg, child, budget, depth + 1)
            out["places"].extend(sub["places"])
            out["calls"] += sub["calls"]
            out["cache_hits"] += sub["cache_hits"]
            out["tiles"] += sub["tiles"]
            out["max_depth"] = max(out["max_depth"], sub["max_depth"])
            for k in ("refused", "budget_hit"):
                if sub.get(k):
                    out[k] = sub[k]
            if out.get("refused") or out.get("budget_hit"):
                break
    return out


# ── normalization ──────────────────────────────────────────────────────────
def _hours(place: dict):
    oh = place.get("regularOpeningHours") or {}
    periods = oh.get("periods") or []
    if not periods:
        return ("hours n/a", None, None, False, None)
    if len(periods) == 1 and "close" not in periods[0]:
        return ("24 hours", 0, 1440, True, True)
    opens, closes = [], []
    for p in periods:
        o, c = p.get("open"), p.get("close")
        if not o or not c:
            continue
        om = o.get("hour", 0) * 60 + o.get("minute", 0)
        cm = c.get("hour", 0) * 60 + c.get("minute", 0)
        if c.get("day") != o.get("day") or cm <= om:
            cm += 1440
        opens.append(om); closes.append(cm)
    if not opens:
        return ("hours n/a", None, None, False, None)
    # Qualification must use the most restrictive reported day rather than an
    # optimistic composite assembled from different days of the week.
    om, cm = max(opens), min(closes)
    hm = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}"
    return (f"{hm(om)}–{hm(cm)}", om, cm, False, om <= 600 and cm >= 1260)


def _segment(place: dict, fallback: str) -> str:
    t = place.get("primaryType") or ""
    if t in _TYPE_SEG:
        return _TYPE_SEG[t]
    name = (place.get("displayName") or {}).get("text", "")
    for seg, rx in _NAME_SEG:
        if rx.search(name):
            return seg
    return fallback


def normalize(place: dict, fallback_seg: str) -> dict:
    loc = place.get("location") or {}
    label, om, cm, h24, meets = _hours(place)
    photos = place.get("photos") or []
    return {
        "place_id": place.get("id"),
        "name": (place.get("displayName") or {}).get("text", "(unnamed)"),
        "address": place.get("formattedAddress", ""),
        "lat": loc.get("latitude"), "lng": loc.get("longitude"),
        "seg": _segment(place, fallback_seg), "type": place.get("primaryType", ""),
        "hours": label, "open_min": om, "close_min": cm,
        "is_24h": h24, "hours_ok": meets,
        "phone": place.get("nationalPhoneNumber", ""),
        "website": place.get("websiteUri", ""),
        "rating": place.get("rating"), "reviews": place.get("userRatingCount", 0) or 0,
        "status_biz": place.get("businessStatus", ""),
        "photo_ref": photos[0].get("name") if photos else None,
        # indoor retail space (an ATM needs somewhere to stand) and whether an
        # ATM is already listed here — the check BD used to do by eye
        "has_shop": shop_presence(place, (place.get("displayName") or {}).get("text", "")),
        # Generic cash machine: neutral-to-positive, NOT a competitor signal.
        "has_atm": "atm" in set(place.get("types") or []),
        "btc_competitor": btc_competitor((place.get("displayName") or {}).get("text", "")),
    }


# ── co-located site merging ────────────────────────────────────────────────
# Google lists a fuel station and its attached shop as SEPARATE places with
# different place_ids at the SAME street address (verified: "Shell"/"Food Mart"
# 27 m apart, "7-Eleven"/"7-Eleven Fuel" 13 m apart — both pairs sharing an
# address). De-duplicating on place_id cannot catch that, so one physical host
# site showed up as two candidates. Group them into one site instead.
CO_LOCATED_M = 30.0
_SUITE_RX = re.compile(r"\b(ste|suite|unit|apt|#)\s*[\w-]+", re.I)


def _norm_addr(a: str) -> str:
    a = (a or "").lower().split(",")
    if not a or not a[0].strip():
        return ""
    street = _SUITE_RX.sub("", a[0])
    street = re.sub(r"[^a-z0-9 ]", " ", street)
    return re.sub(r"\s+", " ", street).strip()


def cluster_sites(recs: list[dict]) -> list[dict]:
    """Merge records that are the same physical site into one candidate."""
    used, clusters = set(), []
    for i, a in enumerate(recs):
        if i in used:
            continue
        group, na = [a], _norm_addr(a.get("address"))
        used.add(i)
        for j in range(i + 1, len(recs)):
            if j in used:
                continue
            b = recs[j]
            same_addr = bool(na) and na == _norm_addr(b.get("address"))
            close = haversine_m((a["lat"], a["lng"]), (b["lat"], b["lng"])) <= CO_LOCATED_M
            if same_addr and close:          # both signals: address AND proximity
                group.append(b); used.add(j)
        clusters.append(_merge_group(group))
    return clusters


def _merge_group(group: list[dict]) -> dict:
    if len(group) == 1:
        return group[0]
    # Primary = the most prominent listing (review count), which is normally the
    # brand a host would recognise; the rest are recorded as also_here.
    group = sorted(group, key=lambda r: (r.get("reviews") or 0), reverse=True)
    main = dict(group[0])
    others = group[1:]
    main["also_here"] = [{"name": o["name"], "seg": o["seg"],
                          "place_id": o["place_id"]} for o in others]
    main["merged_ids"] = [o["place_id"] for o in others]
    # Hours: take the MOST RESTRICTIVE window across the co-located tenants.
    # The machine lives inside the retail space, so a 24 h fuel pump attached to
    # a shop that closes at 23:00 is only accessible until 23:00. Taking the
    # generous side would overstate accessibility and put machines in sites that
    # actually fail the open-late rule.
    known = [o for o in group if o.get("open_min") is not None]
    all_24h = bool(group) and all(o.get("is_24h") for o in group)
    if all_24h:
        main.update({"is_24h": True, "hours_ok": True, "hours": "24 hours"})
    elif known:
        open_min = max(o["open_min"] for o in known)     # latest to open
        close_min = min(o["close_min"] for o in known)   # earliest to close
        hm = lambda m: "%02d:%02d" % ((m // 60) % 24, m % 60)
        main.update({
            "is_24h": False,
            "open_min": open_min, "close_min": close_min,
            "hours": f"{hm(open_min)}–{hm(close_min)}",
            "hours_ok": open_min <= 600 and close_min >= 1260,
        })
        # Keep the per-tenant hours visible so the narrowing is explainable.
        main["hours_detail"] = [{"name": o["name"], "hours": o.get("hours")}
                                for o in group]
    else:
        main.update({"is_24h": False, "hours_ok": None, "hours": "hours n/a"})
    for f in ("phone", "website", "photo_ref", "rating"):     # fill gaps from tenants
        if not main.get(f):
            main[f] = next((o.get(f) for o in group if o.get(f)), main.get(f))
    # A separately-listed shop at the same address is the strongest proof that a
    # forecourt actually has indoor retail space (this is the ARCO + ampm case).
    if any(o.get("has_shop") == "yes" or o.get("seg") in
           ("convenience", "smoke", "liquor") for o in group):
        main["has_shop"] = "yes"
    main["has_atm"] = any(o.get("has_atm") for o in group)
    # A co-located tenant's name counts too: the competitor machine may sit in the
    # attached shop rather than the record we matched on.
    main["btc_competitor"] = ("likely"
                              if any(o.get("btc_competitor") == "likely" for o in group)
                              or btc_competitor(" ".join(o.get("name", "") for o in group))
                                 == "likely"
                              else "unknown")
    return main


# NOTE: star-candidate scoring was removed. It was never wired into /api/search,
# and its config path pointed at star_criteria.example.json, which no longer
# exists. The thresholds are proprietary and deliberately not in this repo — see
# REVISIT_AND_IMPORT.md before rebuilding it.


def haversine_m(a, b) -> float:
    R = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ── search ─────────────────────────────────────────────────────────────────
def _cluster_visible(out: dict, sites: dict,
                     include_hidden: bool) -> tuple[list[dict], int]:
    """Merge physical locations, then apply saved-site visibility consistently."""
    raw = list(out.values())
    merged = cluster_sites(raw)
    if not include_hidden:
        # A merged site is hidden if ANY of its tenants is rejected.
        def hidden(rec):
            group = [rec] + [{"place_id": o["place_id"]}
                             for o in rec.get("also_here", [])]
            return any((sites.get(g["place_id"]) or {}).get("status") in STATUS_HIDE
                       for g in group)
        merged = [r for r in merged if not hidden(r)]
    return merged, len(raw) - len(merged)


def search(lat: float, lng: float, radius_m: float, segments: list[str],
           include_hidden: bool = False, progress=None) -> dict:
    segs = [s for s in (segments or list(SEGMENT_QUERIES)) if s in SEGMENT_QUERIES]
    sites = load_sites()
    out, calls, hits, refused = {}, 0, 0, None
    budget = {"calls": 0, "max": MAX_CALLS_PER_SEARCH}
    coverage = {}
    root_rect = circle_to_rect(lat, lng, radius_m)

    def failure(code: str, detail: str, failed_segment: str) -> dict:
        """Return useful checkpointed work as well as the provider failure."""
        partial, merged_n = _cluster_visible(out, sites, include_hidden)
        payload = {
            "error": code,
            "detail": detail,
            "failed_segment": failed_segment,
            "results": partial,
            "api_calls": calls,
            "cache_hits": hits,
            "merged_duplicates": merged_n,
            "coverage": dict(coverage),
            "truncated_segments": [s for s, c in coverage.items()
                                   if c["truncated"]],
            "tiles": sum(c["tiles"] for c in coverage.values()),
            "status": status(),
        }
        if progress:
            progress(payload)
        return payload

    for seg in segs:
        try:
            res = _adaptive(seg, root_rect, budget)
        except urllib.error.HTTPError as e:
            return failure(f"places_http_{e.code}", e.read().decode()[:400], seg)
        except Exception as e:
            return failure("places_unreachable", str(e)[:200], seg)
        calls += res["calls"]
        hits += res["cache_hits"]
        # A: report whether this segment's coverage is complete or truncated
        coverage[seg] = {
            "found": len(res["places"]), "tiles": res["tiles"],
            "calls": res["calls"], "depth": res["max_depth"],
            "truncated": bool(res.get("saturated") and
                              (res["max_depth"] >= MAX_DEPTH or res.get("budget_hit")
                               or res.get("refused"))),
        }
        if res.get("refused"):
            refused = res["refused"]
        if res.get("budget_hit"):
            refused = (f"per-search call budget reached "
                       f"({MAX_CALLS_PER_SEARCH}) — narrow the area for full coverage")
        for p in res["places"]:
            rec = normalize(p, seg)
            if rec["lat"] is None or not rec["place_id"]:
                continue
            # Tiles are the circle's bounding box, so corners fall outside it.
            # Clip to the radius the user drew — the UI draws exactly that shape.
            if haversine_m((lat, lng), (rec["lat"], rec["lng"])) > radius_m:
                continue
            saved = sites.get(rec["place_id"])
            if saved:
                rec["saved"] = {k: saved.get(k) for k in
                                ("status", "reason", "owner", "next_action",
                                 "followup_date", "notes", "stage", "shortlist")}
            # NOTE: hidden sites are kept here and filtered after clustering —
            # dropping them now would split a merged pair and resurface the
            # rejected site under its co-tenant's name.          # rejected / do-not-contact never reappear
            out.setdefault(rec["place_id"], rec)
        if progress:
            partial, merged_n = _cluster_visible(out, sites, include_hidden)
            progress({
                "results": partial,
                "api_calls": calls,
                "cache_hits": hits,
                "merged_duplicates": merged_n,
                "coverage": dict(coverage),
                "truncated_segments": [s for s, c in coverage.items()
                                       if c["truncated"]],
                "tiles": sum(c["tiles"] for c in coverage.values()),
            })
    # manually-added sites inside the radius always appear
    for sid, rec in sites.items():
        if not rec.get("manual") or rec.get("lat") is None:
            continue
        if haversine_m((lat, lng), (rec["lat"], rec["lng"])) > radius_m:
            continue
        if not include_hidden and rec.get("status") in STATUS_HIDE:
            continue
        out.setdefault(sid, {
            "place_id": sid, "name": rec.get("name", "(manual)"),
            "address": rec.get("address", ""), "lat": rec["lat"], "lng": rec["lng"],
            "seg": rec.get("seg", "other"), "type": "manual", "hours": "hours n/a",
            "open_min": None, "close_min": None, "is_24h": False, "hours_ok": None,
            "phone": rec.get("phone", ""), "website": "", "rating": None, "reviews": 0,
            "status_biz": "", "photo_ref": None, "manual": True,
            "saved": {k: rec.get(k) for k in ("status", "reason", "owner",
                                              "next_action", "followup_date",
                                              "notes", "stage", "shortlist")},
        })
    merged, merged_n = _cluster_visible(out, sites, include_hidden)
    res = {"results": merged, "api_calls": calls, "cache_hits": hits,
           "merged_duplicates": merged_n,
           "coverage": coverage,
           "truncated_segments": [s for s, c in coverage.items() if c["truncated"]],
           "tiles": sum(c["tiles"] for c in coverage.values()),
           "status": status()}
    if refused:
        res["refused"] = refused
    return res


def point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    """Even/odd containment test for a client-drawn [lat, lng] ring."""
    inside = False
    for i in range(len(polygon)):
        j = i - 1
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > lng) != (yj > lng)
                and lat < (xj - xi) * (lng - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
    return inside


def parse_search_params(body: dict) -> dict:
    """Validate and normalize a browser search before allocating its run ID."""
    try:
        lat, lng = float(body.get("lat")), float(body.get("lng"))
        radius = min(float(body.get("radius_m", 2400)), MAX_RADIUS_M)
    except (TypeError, ValueError):
        raise ValueError("bad_params")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        raise ValueError("bad_coords")
    if radius <= 0:
        raise ValueError("bad_radius")
    supplied = body.get("segments") or []
    if isinstance(supplied, str):
        supplied = supplied.split(",")
    segments = list(dict.fromkeys(s for s in supplied if s in SEGMENT_QUERIES))
    if not segments:
        raise ValueError("no_segments")
    polygon = []
    for point in body.get("polygon") or []:
        if isinstance(point, dict):
            point = [point.get("lat"), point.get("lng")]
        try:
            plat, plng = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError("bad_polygon")
        if not (-90 <= plat <= 90 and -180 <= plng <= 180):
            raise ValueError("bad_polygon")
        polygon.append([plat, plng])
        if len(polygon) > 2000:
            raise ValueError("polygon_too_large")
    if polygon and len(polygon) < 3:
        raise ValueError("bad_polygon")
    mode = body.get("mode") if body.get("mode") in {"zip", "pin", "draw"} else "pin"
    zipcode = str(body.get("zip") or "").strip()
    return {
        "mode": mode,
        "zip": zipcode if re.fullmatch(r"\d{5}", zipcode) else "",
        "zip_shape": "boundary" if body.get("zip_shape") == "boundary" else "radius",
        "label": str(body.get("label") or "Search").strip()[:120] or "Search",
        "lat": lat, "lng": lng, "radius_m": radius,
        "segments": segments,
        "include_hidden": bool(body.get("include_hidden")),
        "polygon": polygon,
    }


def _clip_run_payload(payload: dict, polygon: list[list[float]]) -> dict:
    out = dict(payload)
    if polygon and "results" in out:
        out["results"] = [r for r in out.get("results") or []
                          if point_in_polygon(r["lat"], r["lng"], polygon)]
        out["result_count"] = len(out["results"])
    if "api_calls" in out:
        out["estimated_cost_usd"] = round(
            int(out.get("api_calls") or 0) * SKU["search"]["per_1k"] / 1000, 4)
    return out


def execute_search_run(params: dict) -> dict:
    """Run one search with durable checkpoints and a human-shareable ID."""
    run = store.create_search_run(params, source_version=APP_VERSION)
    code = run["search_code"]
    polygon = params.get("polygon") or []

    def checkpoint(payload):
        store.checkpoint_search_run(code, _clip_run_payload(payload, polygon))

    try:
        result = search(params["lat"], params["lng"], params["radius_m"],
                        params["segments"], params["include_hidden"],
                        progress=checkpoint)
        result = _clip_run_payload(result, polygon)
        if result.get("error"):
            run_status = "failed"
        elif result.get("refused"):
            run_status = "stopped_budget"
        elif result.get("truncated_segments"):
            run_status = "partial"
        else:
            run_status = "complete"
        saved = store.checkpoint_search_run(
            code, result, run_status=run_status,
            error_code=result.get("error"), error_detail=result.get("detail"))
    except Exception as exc:
        saved = store.checkpoint_search_run(
            code, {}, run_status="failed", error_code="search_failed",
            error_detail=str(exc))
        result = {"error": "search_failed", "detail": str(exc)[:200],
                  "status": status()}
    result.update({"search_id": code, "run_status": saved["status"],
                   "repeated_from": saved.get("repeated_from")})
    return result


# ── ZIP (ZCTA) lookup by point ─────────────────────────────────────────────
# US Census TIGERweb: keyless, free, and returns the ZIP code AND its true
# boundary polygon in one call — so this costs nothing and never touches the
# Google quota. Note ZCTA != ZIP exactly: USPS ZIPs are delivery routes, not
# areas; ZCTAs are the Census's areal approximation (the standard proxy for
# territory work). PO-box-only ZIPs have no ZCTA and return no feature.
ZCTA_URL = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
            "PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1/query")


def zip_at(lat: float, lng: float) -> dict:
    ck = f"zcta|{lat:.3f}|{lng:.3f}"            # ~110 m granularity
    c = cache_get(ck)
    if c is not None:
        record_cache_hit()
        return c
    q = urllib.parse.urlencode({
        "geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ZCTA5,AREALAND", "returnGeometry": "true",
        "outSR": "4326", "f": "geojson"})
    try:
        req = urllib.request.Request(ZCTA_URL + "?" + q,
                                     headers={"User-Agent": "atm-site-map/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except Exception as e:
        return {"error": "zcta_unreachable", "detail": str(e)[:200]}
    feats = d.get("features") or []
    if not feats:
        out = {"error": "no_zcta",
               "detail": "No ZIP tabulation area covers this point "
                         "(water, unpopulated land, or a PO-box-only ZIP)."}
    else:
        p = feats[0].get("properties") or {}
        try:                                   # AREALAND arrives as a string
            land = float(p.get("AREALAND") or 0)
        except (TypeError, ValueError):
            land = 0.0
        out = {"zip": p.get("ZCTA5"),
               "area_sq_mi": round(land / 2_589_988.0, 2) if land else None,
               "geometry": feats[0].get("geometry")}
    cache_put(ck, out)
    return out


def geocode_zip(z: str) -> dict:
    ck = f"geo|{z}"
    c = cache_get(ck)
    if c is not None:
        record_cache_hit()
        return c
    reason = guard()
    if reason:
        return {"error": "refused", "detail": reason}
    q = urllib.parse.urlencode({"components": f"postal_code:{z}|country:US", "key": _key()})
    try:
        with urllib.request.urlopen(GEOCODE_URL + "?" + q, timeout=25) as r:
            d = json.load(r)
    except Exception as e:
        return {"error": "geocode_unreachable", "detail": str(e)[:200]}
    record_call("geocode")
    res = d.get("results") or []
    out = ({"error": "not_found"} if not res else
           {"lat": res[0]["geometry"]["location"]["lat"],
            "lng": res[0]["geometry"]["location"]["lng"],
            "label": res[0].get("formatted_address", z)})
    cache_put(ck, out)
    return out


# ── HTTP ───────────────────────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        pass            # default access log suppressed; see _access() instead

    def _client_ip(self) -> str:
        """Behind cloudflared every socket is 127.0.0.1; the real visitor
        arrives in CF-Connecting-IP, with X-Forwarded-For as a fallback."""
        return (self.headers.get("CF-Connecting-IP")
                or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0])

    def send_response(self, code, message=None):
        """Every reply passes through here, so it is the one hook that catches
        static files and 401s as well as JSON."""
        super().send_response(code, message)
        if not getattr(self, "_logged", False):
            self._access(code)

    def _access(self, status_code: int, extra: str = "") -> None:
        """Structured request log. Query strings are dropped entirely rather
        than filtered — that is what keeps the API key out of the log."""
        if getattr(self, "_logged", False):
            return                      # _json already wrote the richer line
        self._logged = True
        try:
            path = self.path.split("?")[0]
            ms = int((time.time() - getattr(self, "_t0", time.time())) * 1000)
            ip = self._client_ip()
            ua = (self.headers.get("User-Agent") or "-").replace("\t", " ")[:120]
            line = (f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{ip}\t{self.command}\t"
                    f"{path}\t{status_code}\t{ms}ms\t{ua}\t{extra}".rstrip())
            print(line, flush=True)
            try:
                with open(VISIT_LOG, "a") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass                    # logging must never break a response
        except Exception:
            pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        extra = ""
        if isinstance(obj, dict):
            bits = [f"{k}={obj[k]}" for k in ("search_id", "api_calls", "cache_hits", "advanced")
                    if k in obj]
            if obj.get("error"):
                bits.append(f"error={obj['error']}")
            extra = " ".join(bits)
        self._access(code, extra)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # Allowlist, not a denylist: this handler otherwise serves the whole
    # project directory, which exposed sites.db, sites.db-wal, store.py and
    # crm_adapter.py. A denylist here is always one file behind.
    ALLOWED_FILES = {"/", "/index.html", "/atms.js", "/pipeline.js", "/favicon.ico"}
    ALLOWED_DIRS = ("/prototype/", "/screenshots/")
    ALLOWED_SUFFIX = (".html", ".js", ".css", ".png", ".jpg", ".svg", ".ico", ".mjs")

    def _static_allowed(self, path: str) -> bool:
        if ".." in path:
            return False
        if path in self.ALLOWED_FILES:
            return True
        if path.startswith(self.ALLOWED_DIRS):
            return path.endswith("/") or path.endswith(self.ALLOWED_SUFFIX)
        return False

    def do_GET(self):
        self._t0 = time.time()
        self._logged = False
        u = urllib.parse.urlparse(self.path)
        # Deliberately public and data-free so an external uptime service can
        # distinguish a healthy app from a reachable tunnel without receiving
        # the demo password.
        if u.path == "/healthz":
            return self._json({"ok": True})
        if not _auth_ok(self.headers.get("Authorization")):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ATM Site Map"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Reject traversal syntax before API routing.  Otherwise a path such as
        # /api/../.env is treated as an unknown API route instead of a blocked
        # attempt, weakening both the access boundary and its audit signal.
        if ".." in urllib.parse.unquote(u.path):
            return self._json({"error": "forbidden"}, 403)
        if not u.path.startswith("/api/") and not self._static_allowed(u.path):
            return self._json({"error": "forbidden"}, 403)
        q = urllib.parse.parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]

        if u.path == "/api/crm/status":
            return self._json(crm.status() if crm else {"inbound_enabled": False,
                                                        "outbound_enabled": False})

        if u.path == "/api/config":
            # Only the browser-safe Maps JS key is ever exposed. The Places key
            # (GOOGLE_MAPS_API_KEY) must never leave the server.
            return self._json({"maps_browser_key": os.getenv("MAPS_BROWSER_KEY", ""),
                               "max_radius_m": MAX_RADIUS_M})

        if u.path == "/api/status":
            return self._json(status())

        if u.path == "/api/sites":
            sites = load_sites()
            # Fold in the rich fields so the Saved/Revisit views show a shop the
            # same way the search results do.
            for pid, meta in store.meta_many(sites.keys()).items():
                sites[pid] = {**meta, **sites[pid], "enriched_at": meta.get("enriched_at")}
            return self._json({"sites": sites})

        if u.path == "/api/meta":
            # No ids = everything. The front end needs the whole set on load to
            # paint ATM and pipeline popups, and this table stays small.
            ids = [i for i in (q.get("ids", [""])[0]).split(",") if i]
            return self._json({"meta": store.meta_many(ids) if ids else store.all_meta(),
                               "atm_links": store.atm_links()})

        if u.path == "/api/vocab":
            # reason codes + statuses + stage order, so the UI never hard-codes them
            return self._json({"reason_codes": store.REASON_CODES,
                               "statuses": store.STATUSES,
                               "stage_rank": store.STAGE_RANK})

        if u.path == "/api/revisit":
            return self._json({"due": store.revisit_due()})

        if u.path == "/api/campaigns":
            return self._json({"campaigns": store.campaigns()})

        if u.path == "/api/search-runs":
            try:
                limit = int(one("limit", "100"))
            except ValueError:
                return self._json({"error": "bad_limit"}, 400)
            return self._json({"runs": store.search_runs(
                limit=limit, query=one("q", "") or "",
                run_status=one("status", "") or "")})

        run_match = re.fullmatch(r"/api/search-runs/(S-[0-9A-Z]{4}-[0-9A-Z]{4})",
                                 u.path, re.I)
        if run_match:
            run = store.get_search_run(run_match.group(1).upper())
            return self._json(run if run else {"error": "not_found"},
                              200 if run else 404)

        if u.path == "/api/history":
            pid = one("place_id") or ""
            return self._json({"decisions": store.history(pid),
                               "events": store.events(pid)})

        if u.path == "/api/changes":
            # incremental pull for any external consumer (CRM sync, BI)
            return self._json({"sites": store.since(one("since", "1970-01-01"),
                                                    int(one("limit", "500")))})

        if u.path == "/api/zip-at":
            # Parse params separately from the lookup, so a bug inside zip_at()
            # is never mislabelled as a bad request.
            try:
                zlat, zlng = float(one("lat")), float(one("lng"))
            except (TypeError, ValueError):
                return self._json({"error": "bad_params"}, 400)
            return self._json(zip_at(zlat, zlng))

        if u.path == "/api/geocode":
            z = (one("zip") or "").strip()
            if not re.fullmatch(r"\d{5}", z):
                return self._json({"error": "bad_zip"}, 400)
            return self._json(geocode_zip(z))

        if u.path == "/api/search":
            try:
                lat, lng = float(one("lat")), float(one("lng"))
                radius = min(float(one("radius", "2400")), MAX_RADIUS_M)
            except (TypeError, ValueError):
                return self._json({"error": "bad_params"}, 400)
            # Validate before dispatching: Google rejects out-of-range coordinates
            # anyway, but only after a round-trip, and a negative radius silently
            # builds an inverted rectangle that matches nothing.
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                return self._json({"error": "bad_coords",
                                   "detail": "lat must be -90..90, lng -180..180"}, 400)
            if radius <= 0:
                return self._json({"error": "bad_radius",
                                   "detail": "radius must be greater than 0"}, 400)
            segs = [s for s in (one("segments", "") or "").split(",") if s]
            # Compatibility route for older clients. It still creates a durable
            # run; new clients use POST /api/search/run so the operation is not
            # disguised as a read.
            params = parse_search_params({
                "mode": "pin", "label": f"{lat:.4f}, {lng:.4f}",
                "lat": lat, "lng": lng, "radius_m": radius,
                "segments": segs or list(SEGMENT_QUERIES),
                "include_hidden": one("include_hidden") == "1"})
            return self._json(execute_search_run(params))

        if u.path == "/api/photo":
            ref = one("ref")
            if not ref or not ref.startswith("places/"):
                return self._json({"error": "bad_ref"}, 400)
            width = one("w", "400")
            # Photos and Enterprise Text Search each have a 1,000/month
            # free-usage cap. A shop's photo rarely changes, so cache the bytes
            # on disk and avoid buying the same asset on every review --
            # browser caching alone still re-charges for every new visitor.
            cached = photo_cache_get(ref, width)
            if cached is not None:
                data, ctype = cached
                record_cache_hit()
            else:
                reason = guard()
                if reason:
                    return self._json({"error": "refused", "detail": reason}, 429)
                url = (f"https://places.googleapis.com/v1/{urllib.parse.quote(ref)}/media"
                       f"?maxWidthPx={urllib.parse.quote(width)}"
                       f"&key={urllib.parse.quote(_key())}")
                try:
                    with urllib.request.urlopen(url, timeout=30) as r:
                        data, ctype = r.read(), r.headers.get("Content-Type", "image/jpeg")
                    record_call("photo")
                    photo_cache_put(ref, width, data, ctype)
                except Exception:
                    return self._json({"error": "photo_unavailable"}, 502)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if u.path.startswith("/api/"):
            return self._json({"error": "not_found"}, 404)
        return super().do_GET()

    def do_POST(self):
        self._t0 = time.time()
        self._logged = False
        if not _auth_ok(self.headers.get("Authorization")):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ATM Site Map"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        u = urllib.parse.urlparse(self.path)
        if ".." in urllib.parse.unquote(u.path):
            return self._json({"error": "forbidden"}, 403)
        body = self._body()

        if u.path == "/api/kill":
            trip(f"manual: {body.get('reason','user')}")
            return self._json(status())

        if u.path == "/api/unkill":
            clear_kill()
            return self._json(status())

        if u.path == "/api/site":
            sid = (body.get("id") or "").strip()
            if not sid:
                return self._json({"error": "missing_id"}, 400)
            # The client already holds the full Places record at this moment.
            # Capture it: re-fetching these fields later would cost a call.
            if any(k in body for k in store.META_FIELDS):
                store.upsert_meta(sid, body, source="search")
            return self._json(upsert_site(sid, body))

        if u.path == "/api/search/run":
            try:
                params = parse_search_params(body)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json(execute_search_run(params))

        if u.path == "/api/enrich":
            # Paid, opt-in, batched. Resolves export-only records (existing ATMs,
            # manually added shops) to a Google place and caches the result for
            # good. One Text Search call per unresolved item, budget-capped.
            items = body.get("items") or []
            if not isinstance(items, list) or not items:
                return self._json({"error": "no_items"}, 400)
            budget = {"calls": 0, "max": min(int(body.get("max_calls") or 40),
                                             MAX_CALLS_PER_SEARCH)}
            links, done, skipped, failed = store.atm_links(), [], [], []
            for it in items[:budget["max"]]:
                sn = str(it.get("sn") or it.get("id") or "").strip()
                prior = links.get(sn)
                if prior and prior.get("place_id"):
                    skipped.append(sn)
                    continue
                if budget["calls"] >= budget["max"]:
                    break
                blocked = guard()
                if blocked:
                    return self._json({"error": "killswitch", "detail": blocked,
                                       "resolved": done, "skipped": skipped}, 429)
                pid, meta, conf = resolve_place(it, budget)
                if pid:
                    store.upsert_meta(pid, meta, source="lookup")
                    if sn:
                        store.link_atm(sn, pid, conf)
                    done.append({"sn": sn, "place_id": pid, "confidence": conf})
                else:
                    if sn:
                        store.link_atm(sn, None, "none")
                    failed.append(sn)
            return self._json({"resolved": done, "already_linked": skipped,
                               "unresolved": failed, "calls": budget["calls"],
                               "usage": status()})

        if u.path == "/api/crm/webhook":
            # CRM pushes stage/status changes here. Requires the shared secret
            # in X-CRM-Token; see crm_adapter.py for the field mapping.
            if not crm or not crm.is_enabled():
                return self._json({"error": "crm_not_configured"}, 503)
            if not crm.check_token(self.headers.get("X-CRM-Token")):
                return self._json({"error": "unauthorized"}, 401)
            events = body if isinstance(body, list) else [body]
            applied, skipped = [], []
            for ev in events:
                match, patch = crm.parse_inbound(ev if isinstance(ev, dict) else {})
                if not match or not patch:
                    skipped.append({"event": ev, "why": "no id or no mappable fields"})
                    continue
                # Indexed lookups only — this used to read the whole sites table
                # once per event, i.e. O(events x sites) for a batch.
                target = match
                if store.get(match) is None:
                    row = store.conn().execute(
                        "SELECT place_id FROM sites WHERE crm_id=? LIMIT 1",
                        (match,)).fetchone()
                    if row:
                        target = row["place_id"]
                patch["crm_synced_at"] = datetime.now().isoformat(timespec="seconds")
                upsert_site(target, patch)
                applied.append(target)
            return self._json({"applied": applied, "skipped": skipped,
                               "count": len(applied)})

        if u.path == "/api/campaign/create":
            name = (body.get("name") or "").strip()
            ids = body.get("place_ids") or []
            if not name or not ids:
                return self._json({"error": "need name and place_ids"}, 400)
            return self._json(store.create_campaign(
                name, ids, body.get("area_note", ""), body.get("cost_cents")))

        if u.path == "/api/campaign/transition":
            cid = (body.get("campaign_id") or "").strip()
            to = (body.get("to") or "").strip()
            if not cid or to not in store.STAGE_RANK:
                return self._json({"error": "need campaign_id and a valid stage"}, 400)
            return self._json(store.transition_campaign(
                cid, to, body.get("effective_date"), body.get("vendor_ref")))

        if u.path == "/api/site/delete":
            # Forget a saved record entirely. The shop then returns to normal
            # search results as an unmarked prospect. (With a CRM attached this
            # should become a tombstone instead — see DATA_ARCHITECTURE.md.)
            sid = (body.get("id") or "").strip()
            if not sid:
                return self._json({"error": "missing_id"}, 400)
            removed = store.get(sid)
            if removed is None:
                return self._json({"error": "not_found"}, 404)
            store.delete(sid)
            if crm:
                try:
                    crm.notify("site.deleted", {**removed, "id": sid})
                except Exception:
                    pass
            return self._json({"deleted": sid})

        if u.path == "/api/site/manual":
            name = (body.get("name") or "").strip()
            try:
                lat, lng = float(body["lat"]), float(body["lng"])
            except Exception:
                return self._json({"error": "bad_coords"}, 400)
            if not name:
                return self._json({"error": "missing_name"}, 400)
            sid = "manual_" + hashlib.sha256(
                f"{name}|{lat:.5f}|{lng:.5f}".encode()).hexdigest()[:12]
            patch = {k: body.get(k) for k in SITE_FIELDS if k in body}
            patch.update({"manual": True, "name": name, "lat": lat, "lng": lng,
                          "status": body.get("status", "prospect")})
            return self._json(upsert_site(sid, patch))

        return self._json({"error": "not_found"}, 404)


if __name__ == "__main__":
    load_env()
    try:
        _key()
    except RuntimeError as e:
        raise SystemExit(f"ERROR: {e}")
    dbinfo = store.init()
    pruned = prune_cache()
    st = status()
    print(f"ATM Site Map proxy → http://127.0.0.1:{PORT}")
    print(f"  store: {dbinfo['sites']} sites in sites.db"
          + (f" (migrated {dbinfo['migrated']} from sites.json)" if dbinfo["migrated"] else ""))
    print(f"  killswitch: {'ACTIVE — ' + st['kill_reason'] if st['killed'] else 'clear'}")
    print(f"  daily cap {DAILY_CALL_CAP} · used today {st['calls_today']} · "
          f"burst {BURST_MAX}/{BURST_WINDOW_S}s · budget ${MONTHLY_BUDGET_USD:.2f}")
    print(f"  app-local estimated spend MTD ${st['est_spend_usd']:.4f} · "
          f"free-search estimate {st['free_remaining']['search']} · "
          f"pricing checked {PRICING_AS_OF}")
    print(f"  cache {CACHE_DIR} (TTL {CACHE_TTL_S//3600}h"
          + (f", pruned {pruned}" if pruned else "") + ")")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
