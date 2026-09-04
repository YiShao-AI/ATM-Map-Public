"""Geocode the historical decommissioned-ATM list with OpenStreetMap Nominatim
and write atms.js for the map app. Nominatim policy: <=1 req/sec, real User-Agent."""
import json, time, sys
import urllib.parse, urllib.request

# (serial, business, address, close, note)
ATMS = [
    ("TYKV000644", "Go Mart", "120 W El Norte Pkwy, Escondido, CA 92026", "12AM", ""),
    ("TYKV001276", "Arco", "33440 Hwy 74, Hemet, CA 92545", "24/7", ""),
    ("TYKA007711", "Almond Smoke Shop", "30145 Antelope Rd, Menifee, CA 92584", "9PM", ""),
    ("TYKV000086", "Arco", "26050 Menifee Rd, Romoland, CA 92585", "", ""),
    ("TYKV025595", "One Stop Mini Mart", "24988 E 3rd St, San Bernardino, CA 92410", "11PM", ""),
    ("TYKV002061", "N&N Smoke Shop", "1470 E Highland Ave, San Bernardino, CA 92404", "9PM", ""),
    ("TYKV001304", "Arco", "3659 Central Ave, Riverside, CA 92506", "11PM", ""),
    ("TYKV015541", "The Smoke Shop", "16960 Van Buren Blvd, Riverside, CA 92504", "10PM", ""),
    ("TYKV017060", "One Stop Liquor", "4300 Green River Rd, Corona, CA 92880", "10PM", ""),
    ("TYKV001569", "AJ Liquor Mart", "112 N Tustin Ave, Anaheim, CA 92807", "10PM", ""),
    ("TYAA013084", "Cleopatra Smoke Shop", "2345 N Tustin St, Orange, CA 92865", "8PM", ""),
    ("TYKV001810", "Market & Tobacco", "5367 Lincoln Ave, Cypress, CA 90630", "8PM", ""),
    ("TYKV002922", "Convenient Market", "3808 10th St, Long Beach, CA 90804", "9PM", ""),
    ("TYKV000962", "76 Gas", "2790 Cherry Ave, Signal Hill, CA 90755", "12AM", ""),
    ("TYKV001562", "Arco", "2820 E Alondra Blvd, Compton, CA 90221", "24/7", ""),
    ("TYKV001578", "ARCO", "1001 W Artesia Blvd, Gardena, CA 90248", "24/7", ""),
    ("TYKV001576", "ARCO", "16518 Hawthorne Blvd, Lawndale, CA 90260", "24/7", ""),
    ("TYKV001816", "ARCO", "4015 W El Segundo Blvd, Hawthorne, CA 90250", "24/7", ""),
    ("TYKV001579", "ARCO", "6300 W Slauson Blvd, Culver City, CA 90230", "24/7", ""),
    ("TYAA004718", "Palm Smoke N More Plus", "11122 Palms Blvd, Los Angeles, CA 90034", "9PM", ""),
    ("TYKV001581", "405 Smoke N More Plus", "11221 National Blvd, Los Angeles, CA 90064", "10PM", ""),
    ("TYKV001565", "ARCO", "3775 S Vermont Ave, Los Angeles, CA 90007", "24/7", ""),
    ("TYKV001567", "Mobil", "5857 W Sunset Blvd, Los Angeles, CA 90028", "24/7", ""),
    ("TYKV001566", "Roy's Liquor", "1627 N San Fernando Blvd, Burbank, CA 91504", "10PM", ""),
    ("TYKV021035", "Ricky's Liquor", "18520 Soledad Canyon Rd, Santa Clarita, CA 91351", "11:30PM", ""),
    ("TYKV001272", "Cig Zone", "17212 Saticoy St, Van Nuys, CA 91406", "7:30PM", ""),
    ("TYKV001631", "Primarily Wines, Spirits & Liquor", "22744 Ventura Blvd, Woodland Hills, CA 91364", "2AM", ""),
    ("TYKV001856", "76 Gas", "501 S Rose Ave, Oxnard, CA 93030", "24/7", ""),
    ("TYKV001575", "ARCO", "144 N Verdugo Rd, Glendale, CA 91206", "24/7", ""),
    ("TYKA001416", "Cig Zone", "141 W California Blvd, Pasadena, CA 91105", "7PM", ""),
    ("TYKA007704", "Peacock Liquor", "419 S 1st Ave, Arcadia, CA 91006", "10PM", ""),
    ("TYKV010638", "Ambassador Inn Hotel", "2720 W Valley Blvd, Alhambra, CA 91803", "24/7", ""),
    ("TYKV002058", "Sweet Dream Smoke and Vape", "946 Cardiff St, San Diego, CA 92114", "", ""),
]

import re
UA = "atm-site-map/1.0 (portfolio contact: repository owner)"

def geocode(addr):
    q = urllib.parse.urlencode({"q": addr, "format": "json", "limit": 1, "countrycodes": "us"})
    req = urllib.request.Request("https://nominatim.openstreetmap.org/search?" + q,
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    if data:
        return float(data[0]["lat"]), float(data[0]["lon"])
    return None

def geocode_fallback(addr):
    """Return (lat, lng, approx). Retry without suite; last resort city+zip centroid."""
    g = geocode(addr)
    if g:
        return g[0], g[1], False
    simple = re.sub(r',?\s*(Suite|Ste|STE|Unit|#)\s*\S+', '', addr, flags=re.I)
    if simple != addr:
        time.sleep(1.1); g = geocode(simple)
        if g:
            return g[0], g[1], False
    m = re.search(r'([A-Za-z .]+),\s*CA\s*(\d{5})', addr)
    if m:
        time.sleep(1.1); g = geocode(f"{m.group(1).strip()}, CA {m.group(2)}")
        if g:
            return g[0], g[1], True   # approximate — city/zip centroid
    return None, None, False

# ── deduplicate by serial number (keep first occurrence) ──
seen, deduped = set(), []
for row in ATMS:
    if row[0] in seen:
        print(f"  dup skipped: {row[0]} {row[1]}", file=sys.stderr); continue
    seen.add(row[0]); deduped.append(row)
print(f"{len(ATMS)} rows -> {len(deduped)} unique ATMs\n", file=sys.stderr)

out = []
for sn, name, addr, close, note in deduped:
    lat = lng = None; approx = False
    try:
        lat, lng, approx = geocode_fallback(addr)
    except Exception as e:
        print(f"  geocode fail {sn}: {e}", file=sys.stderr)
    out.append({"sn": sn, "name": name, "address": addr, "close": close,
                "note": note, "lat": lat, "lng": lng, "approx": approx})
    tag = "MISS" if lat is None else ("~APPROX" if approx else "OK")
    print(f"  {sn}  {name[:26]:26} {tag:8} {addr[:38]}", file=sys.stderr)
    time.sleep(1.1)  # respect Nominatim rate limit

ok = sum(1 for a in out if a["lat"])
open("atms.js", "w").write("window.ATMS = " + json.dumps(out, indent=1) + ";\n")
print(f"\nwrote atms.js — {ok}/{len(out)} geocoded", file=sys.stderr)
