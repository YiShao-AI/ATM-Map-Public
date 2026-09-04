"""
SQLite store for sites, decisions, campaigns, search runs and an append-only
event log.

Replaces the previous sites.json, which was rewritten wholesale on every save —
unsafe as soon as anything else writes, and it kept no history at all. Migration
from sites.json runs automatically on first use; the JSON is left untouched as a
backup.

Design points that matter:

* **Decisions are append-only.** A rejection is never overwritten; a new decision
  supersedes the old one. That is what makes "how many attempts before a yes?"
  answerable, and it lets a bad import be understood after the fact.
* **Reject reasons are a structured code, not free text**, each carrying its own
  revisit interval. A reason's half-life is the whole basis of the revisit queue.
* **The funnel is monotonic** — an update carrying an earlier stage is logged but
  does not move the record backwards. Without this, a mailing partner re-uploading
  a file would knock shops in `negotiating` back to `postcard_mailed`.
* **Campaigns are first-class.** Shops are mailed as a cohort of thousands, so a
  bulk transition is one row update, not thousands of writes.
* **Searches are durable runs.** A short ID is allocated before the first
  provider call, progress is checkpointed, and an interrupted process leaves an
  inspectable partial record instead of disappearing.
"""
from __future__ import annotations
import json, re, secrets, sqlite3, hashlib, threading
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "sites.db"
LEGACY_JSON = ROOT / "sites.json"

_local = threading.local()

# ── vocabulary ─────────────────────────────────────────────────────────────
# Each reject reason carries how long it stays valid. This is the difference
# between a dead list and a revisit pipeline.
REASON_CODES: dict[str, dict] = {
    "owner_declined":  {"label": "Owner declined",             "months": 12,   "permanent": False},
    "no_response":     {"label": "No response to outreach",    "months": 6,    "permanent": False},
    "unreachable":     {"label": "Could not reach a decision-maker", "months": 3, "permanent": False},
    "competitor_atm":  {"label": "Competitor ATM already there", "months": 24, "permanent": False},
    "rate_terms":      {"label": "Declined on rate / terms",   "months": 12,   "permanent": False},
    "chain_hq":        {"label": "Chain — needs HQ approval",  "months": 12,   "permanent": False},
    "safety":          {"label": "Area or site risk",          "months": 24,   "permanent": False},
    "no_indoor_space": {"label": "No indoor space for a machine", "months": None, "permanent": True},
    "closed":          {"label": "Business closed",            "months": None, "permanent": True},
    "other":           {"label": "Other",                      "months": 12,   "permanent": False},
}

STATUSES = ["prospect", "contacted", "rejected", "do_not_contact", "pipeline"]

# Parallel entry points share a rank: a shop reached by phone (`contacted`) is as
# far along as one that answered a postcard (`responded`).
STAGE_RANK = {
    "postcard_mailed": 1, "responded": 2, "contacted": 2, "consultation": 3,
    "negotiating": 4, "contract_sent": 5, "contract_signed": 6,
    "shipment": 7, "operational": 8,
}

SCHEMA = """
PRAGMA journal_mode=WAL;

-- The physical place. Survives tenant changes, so "no indoor space" stays true
-- for whoever trades there next.
CREATE TABLE IF NOT EXISTS locations (
  location_id      TEXT PRIMARY KEY,
  address_norm     TEXT,
  lat REAL, lng REAL,
  structural_block TEXT,
  first_seen       TEXT
);

-- A business occupying a location; one row per Google place_id.
CREATE TABLE IF NOT EXISTS sites (
  place_id      TEXT PRIMARY KEY,
  location_id   TEXT REFERENCES locations(location_id),
  crm_id        TEXT,
  name          TEXT,
  address       TEXT,
  lat REAL, lng REAL,
  seg           TEXT,
  status        TEXT NOT NULL DEFAULT 'prospect',
  stage         TEXT,
  owner         TEXT,
  notes         TEXT,
  next_action   TEXT,
  last_contact  TEXT,
  followup_date TEXT,
  reason        TEXT,          -- free text, kept alongside the code
  reason_code   TEXT,
  revisit_after TEXT,
  permanent     INTEGER DEFAULT 0,
  manual        INTEGER DEFAULT 0,
  shortlist     INTEGER DEFAULT 0,
  still_present INTEGER DEFAULT 1,
  source        TEXT,
  created       TEXT,
  updated       TEXT,
  updated_by    TEXT,
  crm_synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sites_status  ON sites(status);
CREATE INDEX IF NOT EXISTS idx_sites_revisit ON sites(revisit_after);
CREATE INDEX IF NOT EXISTS idx_sites_updated ON sites(updated);
CREATE INDEX IF NOT EXISTS idx_sites_crm     ON sites(crm_id);
CREATE INDEX IF NOT EXISTS idx_sites_loc     ON sites(location_id);

-- Append-only decision history: never overwritten, only superseded.
CREATE TABLE IF NOT EXISTS decisions (
  decision_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  place_id     TEXT NOT NULL,
  status       TEXT NOT NULL,
  stage        TEXT,
  reason_code  TEXT,
  reason_note  TEXT,
  decided_at   TEXT NOT NULL,
  decided_by   TEXT,
  revisit_after TEXT,
  permanent    INTEGER DEFAULT 0,
  superseded   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dec_place ON decisions(place_id, superseded);

-- Audit of every field change, from any source.
CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  place_id  TEXT,
  ts        TEXT NOT NULL,
  actor     TEXT,
  kind      TEXT,
  field     TEXT,
  old_value TEXT,
  new_value TEXT,
  note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_place ON events(place_id, ts);
CREATE INDEX IF NOT EXISTS idx_ev_ts    ON events(ts);

-- Shops are mailed as a cohort, so the cohort is a real object.
CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  area_note   TEXT,
  piece_count INTEGER DEFAULT 0,
  cost_cents  INTEGER,
  status      TEXT DEFAULT 'draft',
  vendor_ref  TEXT,
  created_at  TEXT,
  mailed_at   TEXT
);
CREATE TABLE IF NOT EXISTS campaign_members (
  campaign_id  TEXT NOT NULL,
  place_id     TEXT NOT NULL,
  added_at     TEXT,
  mailed_at    TEXT,
  responded_at TEXT,
  outcome      TEXT,
  PRIMARY KEY (campaign_id, place_id)
);
CREATE INDEX IF NOT EXISTS idx_cm_place ON campaign_members(place_id);

-- Every provider-backed search is an operational record. Parameters and result
-- snapshots are deliberately stored separately from the live prospect table:
-- reopening an old run must show what the operator saw at that time.
CREATE TABLE IF NOT EXISTS search_runs (
  run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
  search_code        TEXT NOT NULL UNIQUE,
  query_key          TEXT NOT NULL,
  label              TEXT,
  status             TEXT NOT NULL,
  started_at         TEXT NOT NULL,
  completed_at       TEXT,
  params_json        TEXT NOT NULL,
  api_calls          INTEGER NOT NULL DEFAULT 0,
  cache_hits         INTEGER NOT NULL DEFAULT 0,
  tile_count         INTEGER NOT NULL DEFAULT 0,
  result_count       INTEGER NOT NULL DEFAULT 0,
  merged_duplicates  INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  truncated_json     TEXT,
  coverage_json      TEXT,
  error_code         TEXT,
  error_detail       TEXT,
  repeated_from      TEXT,
  source_version     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON search_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status  ON search_runs(status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_query   ON search_runs(query_key, started_at DESC);

CREATE TABLE IF NOT EXISTS search_run_results (
  run_id        INTEGER NOT NULL REFERENCES search_runs(run_id) ON DELETE CASCADE,
  ordinal       INTEGER NOT NULL,
  place_id      TEXT,
  snapshot_json TEXT NOT NULL,
  PRIMARY KEY (run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_run_result_place
  ON search_run_results(place_id, run_id);

/* Rich Places metadata, keyed by place_id and shared by every layer: search
   results, pipeline sites and resolved existing ATMs all read from here, so a
   shop looks the same whichever list it is reached from. Kept separate from
   `sites` because an existing ATM is not a prospect and must not create one. */
CREATE TABLE IF NOT EXISTS place_meta (
  place_id    TEXT PRIMARY KEY,
  name        TEXT,
  address     TEXT,
  lat REAL, lng REAL,
  seg         TEXT,
  type        TEXT,
  hours       TEXT,
  open_min    INTEGER,
  close_min   INTEGER,
  is_24h      INTEGER,
  hours_ok    INTEGER,
  phone       TEXT,
  website     TEXT,
  rating      REAL,
  reviews     INTEGER,
  status_biz  TEXT,
  photo_ref   TEXT,
  has_shop    TEXT,
  has_atm     INTEGER,          -- generic cash machine: neutral, not a blocker
  btc_competitor TEXT,          -- 'likely' | 'unknown' -- never 'no'

  source      TEXT,          -- 'search' (free, captured on mark) | 'lookup' (paid)
  enriched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_meta_enriched ON place_meta(enriched_at);

/* Existing ATMs arrive from a company export keyed by serial number with no
   place_id, so resolving one to a Google place is a paid lookup. Cache the
   result permanently — a machine does not move. */
CREATE TABLE IF NOT EXISTS atm_link (
  sn          TEXT PRIMARY KEY,
  place_id    TEXT,
  confidence  TEXT,          -- 'exact' | 'near' | 'none'
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_atmlink_place ON atm_link(place_id);
"""

# Whitelist for place_meta writes: anything else in a payload is ignored.
META_FIELDS = ("name", "address", "lat", "lng", "seg", "type", "hours",
               "open_min", "close_min", "is_24h", "hours_ok", "phone",
               "website", "rating", "reviews", "status_biz", "photo_ref",
               "has_shop", "has_atm", "btc_competitor")

_SUITE = re.compile(r"\b(ste|suite|unit|apt|#)\s*[\w-]+", re.I)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm_address(a: str) -> str:
    head = (a or "").split(",")[0]
    head = _SUITE.sub("", head.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", head)).strip()


def location_id_for(address: str, lat, lng) -> str:
    """Stable id for a physical place: normalised street + coords to ~15 m.
    A new tenant at the same address therefore lands on the same location."""
    key = f"{norm_address(address)}|{round(float(lat or 0),4)}|{round(float(lng or 0),4)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def compute_revisit(reason_code: str | None, when: date | None = None):
    """(revisit_after, permanent) from the reason's half-life."""
    spec = REASON_CODES.get(reason_code or "")
    if not spec:
        return None, 0
    if spec["permanent"] or not spec["months"]:
        return None, 1
    base = when or date.today()
    return (base + timedelta(days=int(spec["months"] * 30.44))).isoformat(), 0


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")   # declared FKs were previously inert
        c.executescript(SCHEMA)
        _migrate(c)
        _local.c = c
    return c


# CREATE TABLE IF NOT EXISTS cannot add a column to a database that already
# exists, so a new field would break every deployed copy until it was rebuilt.
# Each entry is additive and safe to run on every connect.
_MIGRATIONS = [
    ("place_meta", "btc_competitor", "TEXT"),
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, col, decl in _MIGRATIONS:
        try:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if cols and col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        except sqlite3.Error:
            pass


def init() -> dict:
    """Create the schema, migrate sites.json once, and close abandoned runs.

    A process cannot legitimately still be executing a search when a new server
    process starts. Converting those rows here makes a killed worker visible in
    History while preserving every checkpointed result.
    """
    c = conn()
    interrupted = recover_interrupted_search_runs()
    n = c.execute("SELECT COUNT(*) n FROM sites").fetchone()["n"]
    migrated = 0
    if n == 0 and LEGACY_JSON.exists():
        try:
            legacy = json.loads(LEGACY_JSON.read_text())
        except Exception:
            legacy = {}
        for sid, rec in (legacy or {}).items():
            rec = dict(rec or {})
            rec.pop("id", None)
            upsert(sid, rec, actor="migration", log=False)
            migrated += 1
    return {"sites": c.execute("SELECT COUNT(*) n FROM sites").fetchone()["n"],
            "migrated": migrated, "interrupted_runs": interrupted}


# ── reads ──────────────────────────────────────────────────────────────────
_SITE_COLS = ("place_id location_id crm_id name address lat lng seg status stage owner "
              "notes next_action last_contact followup_date reason reason_code "
              "revisit_after permanent manual shortlist still_present source "
              "created updated updated_by crm_synced_at").split()


def _row(r) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["id"] = d.get("place_id")
    return d


def all_sites() -> dict:
    """Keyed by place_id — the shape the rest of the app already expects."""
    return {r["place_id"]: _row(r) for r in conn().execute("SELECT * FROM sites")}


def get(place_id: str):
    r = conn().execute("SELECT * FROM sites WHERE place_id=?", (place_id,)).fetchone()
    return _row(r) if r else None


def upsert_meta(place_id: str, meta: dict, source: str = "search") -> dict:
    """Store rich Places metadata for a place_id.

    Only non-empty values overwrite: a free capture at mark() time must never
    blank out fields a paid lookup already filled in, and vice versa.
    """
    if not place_id:
        return {}
    c = conn()
    with c:
        cur = c.execute("SELECT * FROM place_meta WHERE place_id=?", (place_id,)).fetchone()
        row = {k: cur[k] for k in cur.keys()} if cur else {}
        for k in META_FIELDS:
            v = meta.get(k)
            if v is None or v == "":
                continue
            if isinstance(v, bool):
                v = int(v)
            row[k] = v
        row["place_id"] = place_id
        row["source"] = source
        row["enriched_at"] = _now()
        cols = ["place_id", "source", "enriched_at"] + [k for k in META_FIELDS if k in row]
        c.execute(
            f"INSERT INTO place_meta ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(place_id) DO UPDATE SET "
            f"{','.join(f'{k}=excluded.{k}' for k in cols if k != 'place_id')}",
            [row.get(k) for k in cols])
    return row


def get_meta(place_id: str):
    r = conn().execute("SELECT * FROM place_meta WHERE place_id=?", (place_id,)).fetchone()
    return {k: r[k] for k in r.keys()} if r else None


def meta_many(place_ids) -> dict:
    """Metadata for a batch, keyed by place_id. Chunked past SQLite's var limit."""
    ids, out = [i for i in place_ids if i], {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in conn().execute(
                f"SELECT * FROM place_meta WHERE place_id IN ({','.join('?' * len(chunk))})",
                chunk):
            out[r["place_id"]] = {k: r[k] for k in r.keys()}
    return out


def all_meta() -> dict:
    return {r["place_id"]: {k: r[k] for k in r.keys()}
            for r in conn().execute("SELECT * FROM place_meta")}


def link_atm(sn: str, place_id: str | None, confidence: str = "none") -> None:
    """Record the resolution of an exported ATM serial to a Google place.
    A miss is stored too, so a fruitless lookup is never paid for twice."""
    with conn() as c:
        c.execute(
            "INSERT INTO atm_link (sn, place_id, confidence, resolved_at) VALUES (?,?,?,?) "
            "ON CONFLICT(sn) DO UPDATE SET place_id=excluded.place_id, "
            "confidence=excluded.confidence, resolved_at=excluded.resolved_at",
            (sn, place_id, confidence, _now()))


def atm_links() -> dict:
    return {r["sn"]: {k: r[k] for k in r.keys()}
            for r in conn().execute("SELECT * FROM atm_link")}


def since(ts: str, limit: int = 500) -> list:
    rows = conn().execute(
        "SELECT * FROM sites WHERE updated > ? ORDER BY updated LIMIT ?",
        (ts, limit)).fetchall()
    return [_row(r) for r in rows]


def history(place_id: str) -> list:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM decisions WHERE place_id=? ORDER BY decision_id DESC", (place_id,))]


def events(place_id: str | None = None, limit: int = 200) -> list:
    if place_id:
        q = "SELECT * FROM events WHERE place_id=? ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in conn().execute(q, (place_id, limit))]
    return [dict(r) for r in conn().execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


# ── durable search runs ────────────────────────────────────────────────────
_SEARCH_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TERMINAL_RUN_STATUSES = {"complete", "partial", "failed", "stopped_budget",
                          "interrupted"}


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _query_key(params: dict) -> str:
    """Stable identity for searches that would issue the same provider work."""
    polygon = params.get("polygon") or []
    basis = {
        "lat": round(float(params.get("lat") or 0), 6),
        "lng": round(float(params.get("lng") or 0), 6),
        "radius_m": round(float(params.get("radius_m") or 0), 2),
        "segments": sorted(params.get("segments") or []),
        "include_hidden": bool(params.get("include_hidden")),
        "polygon": [
            [round(float(p[0]), 6), round(float(p[1]), 6)]
            for p in polygon
        ],
    }
    return hashlib.sha256(_json_text(basis).encode()).hexdigest()


def _new_search_code(c: sqlite3.Connection) -> str:
    """Human-friendly 40-bit ID; the unique index is the final authority."""
    for _ in range(20):
        raw = "".join(secrets.choice(_SEARCH_CODE_ALPHABET) for _ in range(8))
        code = f"S-{raw[:4]}-{raw[4:]}"
        if not c.execute("SELECT 1 FROM search_runs WHERE search_code=?", (code,)).fetchone():
            return code
    raise RuntimeError("could not allocate a unique search ID")


def _search_run_row(row, include_results: bool = False) -> dict | None:
    if not row:
        return None
    out = dict(row)
    for raw, public, default in (
            ("params_json", "params", {}),
            ("truncated_json", "truncated_segments", []),
            ("coverage_json", "coverage", {})):
        try:
            out[public] = json.loads(out.pop(raw) or "null") or default
        except (TypeError, json.JSONDecodeError):
            out[public] = default
            out.pop(raw, None)
    if out.get("completed_at"):
        try:
            start = datetime.fromisoformat(out["started_at"])
            end = datetime.fromisoformat(out["completed_at"])
            out["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))
        except (TypeError, ValueError):
            out["duration_ms"] = None
    else:
        out["duration_ms"] = None
    if include_results:
        rows = conn().execute(
            "SELECT snapshot_json FROM search_run_results WHERE run_id=? ORDER BY ordinal",
            (out["run_id"],)).fetchall()
        results = []
        for result in rows:
            try:
                results.append(json.loads(result["snapshot_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
        out["results"] = results
    return out


def create_search_run(params: dict, source_version: str = "") -> dict:
    """Allocate and commit an ID before any external provider call."""
    c = conn()
    key = _query_key(params)
    with c:
        prior = c.execute(
            "SELECT search_code FROM search_runs WHERE query_key=? "
            "AND status IN ('complete','partial','stopped_budget') "
            "ORDER BY run_id DESC LIMIT 1", (key,)).fetchone()
        for _ in range(20):
            code = _new_search_code(c)
            try:
                c.execute(
                    "INSERT INTO search_runs (search_code,query_key,label,status,started_at,"
                    "params_json,repeated_from,source_version) VALUES (?,?,?,?,?,?,?,?)",
                    (code, key, str(params.get("label") or "Search")[:120], "running", _now(),
                     _json_text(params), prior["search_code"] if prior else None,
                     source_version or None))
                break
            except sqlite3.IntegrityError:
                # A concurrent request can win after _new_search_code checks.
                # Generate another public code; the unique index remains the
                # authority rather than relying on probability alone.
                continue
        else:
            raise RuntimeError("could not persist a unique search ID")
    return get_search_run(code, include_results=False)


def checkpoint_search_run(search_code: str, payload: dict,
                          run_status: str = "running",
                          error_code: str | None = None,
                          error_detail: str | None = None) -> dict:
    """Atomically update counters and the latest recoverable result snapshot."""
    c = conn()
    row = c.execute("SELECT * FROM search_runs WHERE search_code=?", (search_code,)).fetchone()
    if not row:
        raise KeyError(search_code)
    terminal = run_status in _TERMINAL_RUN_STATUSES
    current = dict(row)
    vals = {
        "api_calls": int(payload.get("api_calls", current["api_calls"]) or 0),
        "cache_hits": int(payload.get("cache_hits", current["cache_hits"]) or 0),
        "tile_count": int(payload.get("tiles", current["tile_count"]) or 0),
        "result_count": int(payload.get("result_count", current["result_count"]) or 0),
        "merged_duplicates": int(payload.get(
            "merged_duplicates", current["merged_duplicates"]) or 0),
        "estimated_cost_usd": float(payload.get(
            "estimated_cost_usd", current["estimated_cost_usd"]) or 0),
        "truncated_json": _json_text(payload.get("truncated_segments") or [])
            if "truncated_segments" in payload else current["truncated_json"],
        "coverage_json": _json_text(payload.get("coverage") or {})
            if "coverage" in payload else current["coverage_json"],
    }
    results = payload.get("results") if "results" in payload else None
    if results is not None:
        vals["result_count"] = len(results)
    with c:
        c.execute(
            "UPDATE search_runs SET status=?,completed_at=?,api_calls=?,cache_hits=?,"
            "tile_count=?,result_count=?,merged_duplicates=?,estimated_cost_usd=?,"
            "truncated_json=?,coverage_json=?,error_code=?,error_detail=? "
            "WHERE search_code=?",
            (run_status, _now() if terminal else None, vals["api_calls"],
             vals["cache_hits"], vals["tile_count"], vals["result_count"],
             vals["merged_duplicates"], vals["estimated_cost_usd"],
             vals["truncated_json"], vals["coverage_json"], error_code,
             (error_detail or "")[:500] or None, search_code))
        if results is not None:
            run_id = current["run_id"]
            c.execute("DELETE FROM search_run_results WHERE run_id=?", (run_id,))
            c.executemany(
                "INSERT INTO search_run_results (run_id,ordinal,place_id,snapshot_json) "
                "VALUES (?,?,?,?)",
                [(run_id, i, r.get("place_id"), _json_text(r))
                 for i, r in enumerate(results)])
    return get_search_run(search_code, include_results=False)


def get_search_run(search_code: str, include_results: bool = True) -> dict | None:
    row = conn().execute(
        "SELECT * FROM search_runs WHERE search_code=?", (search_code.upper(),)).fetchone()
    return _search_run_row(row, include_results)


def search_runs(limit: int = 100, query: str = "", run_status: str = "") -> list:
    limit = max(1, min(int(limit), 200))
    where, args = [], []
    if query:
        where.append("(search_code LIKE ? OR label LIKE ?)")
        term = f"%{query.strip()}%"
        args.extend([term.upper(), term])
    if run_status:
        where.append("status=?")
        args.append(run_status)
    sql = "SELECT * FROM search_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY run_id DESC LIMIT ?"
    args.append(limit)
    return [_search_run_row(r, include_results=False)
            for r in conn().execute(sql, args)]


def recover_interrupted_search_runs() -> int:
    """Close rows left running by a prior process while keeping checkpoints."""
    c = conn()
    with c:
        cur = c.execute(
            "UPDATE search_runs SET status='interrupted',completed_at=?,"
            "error_code='process_restart',"
            "error_detail='Server restarted before the search completed.' "
            "WHERE status='running'",
            (_now(),))
    return cur.rowcount


def revisit_due(today: str | None = None, limit: int = 1000) -> list:
    """Rejections whose half-life has elapsed, excluding permanent blocks."""
    today = today or date.today().isoformat()
    rows = conn().execute("""
        SELECT s.* FROM sites s
        LEFT JOIN locations l ON l.location_id = s.location_id
        WHERE s.status = 'rejected'
          AND COALESCE(s.permanent,0) = 0
          AND s.revisit_after IS NOT NULL
          AND s.revisit_after <= ?
          AND COALESCE(s.still_present,1) = 1
          AND l.structural_block IS NULL
        ORDER BY s.revisit_after LIMIT ?""", (today, limit)).fetchall()
    return [_row(r) for r in rows]


# ── writes ─────────────────────────────────────────────────────────────────
def _log(c, place_id, actor, kind, field=None, old=None, new=None, note=None):
    c.execute("INSERT INTO events (place_id,ts,actor,kind,field,old_value,new_value,note)"
              " VALUES (?,?,?,?,?,?,?,?)",
              (place_id, _now(), actor, kind, field,
               None if old is None else str(old), None if new is None else str(new), note))


def upsert(place_id: str, patch: dict, actor: str = "user", log: bool = True) -> dict:
    """Apply a patch, enforcing the monotonic funnel and recording history."""
    c = conn()
    with c:
        cur = c.execute("SELECT * FROM sites WHERE place_id=?", (place_id,)).fetchone()
        existing = _row(cur) if cur else {}

        # The funnel only advances. A lower stage is evidence, not a state change.
        blocked = None
        new_stage = patch.get("stage")
        if new_stage and existing.get("stage"):
            if STAGE_RANK.get(new_stage, 0) < STAGE_RANK.get(existing["stage"], 0):
                blocked = new_stage
                patch = {k: v for k, v in patch.items() if k != "stage"}

        # A reject reason sets its own expiry.
        if patch.get("reason_code"):
            ra, perm = compute_revisit(patch["reason_code"])
            patch.setdefault("revisit_after", ra)
            patch.setdefault("permanent", perm)
        # ...and leaving 'rejected' must clear it, or a requalified site keeps a
        # stale permanent flag and would be misclassified by any later query.
        if (patch.get("status") and patch["status"] != "rejected"
                and existing.get("status") == "rejected"):
            patch.setdefault("permanent", 0)
            patch.setdefault("revisit_after", None)
            patch.setdefault("reason_code", None)

        # A rejected or blocked site cannot retain a manual Ready-to-mail
        # approval, regardless of which client or integration wrote the change.
        if patch.get("status") in {"rejected", "do_not_contact"}:
            patch["shortlist"] = 0

        if patch.get("address") or patch.get("lat") is not None:
            addr = patch.get("address", existing.get("address"))
            lat = patch.get("lat", existing.get("lat"))
            lng = patch.get("lng", existing.get("lng"))
            if addr or lat:
                lid = location_id_for(addr or "", lat, lng)
                patch.setdefault("location_id", lid)
                c.execute("INSERT OR IGNORE INTO locations"
                          " (location_id,address_norm,lat,lng,first_seen) VALUES (?,?,?,?,?)",
                          (lid, norm_address(addr or ""), lat, lng, _now()))

        data = {k: v for k, v in patch.items() if k in _SITE_COLS}
        data["place_id"] = place_id
        data["updated"] = _now()
        data["updated_by"] = actor
        if not existing:
            data.setdefault("created", _now())
            data.setdefault("status", "prospect")
            data.setdefault("source", "manual" if patch.get("manual") else "search")

        cols = list(data)
        c.execute(f"INSERT INTO sites ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})"
                  f" ON CONFLICT(place_id) DO UPDATE SET "
                  + ",".join(f"{k}=excluded.{k}" for k in cols if k != "place_id"),
                  [data[k] for k in cols])

        if log:
            for f in ("status", "stage", "reason_code", "owner", "crm_id"):
                if f in data and str(existing.get(f)) != str(data[f]):
                    _log(c, place_id, actor, "field", f, existing.get(f), data[f])
            if blocked:
                _log(c, place_id, actor, "stage_blocked", "stage",
                     existing.get("stage"), blocked,
                     "ignored: funnel only advances")

        # a status change opens a new decision and supersedes the previous one
        if "status" in data and existing.get("status") != data["status"]:
            c.execute("UPDATE decisions SET superseded=1 WHERE place_id=? AND superseded=0",
                      (place_id,))
            c.execute("INSERT INTO decisions (place_id,status,stage,reason_code,reason_note,"
                      "decided_at,decided_by,revisit_after,permanent)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (place_id, data["status"], data.get("stage", existing.get("stage")),
                       data.get("reason_code"), data.get("reason"), _now(), actor,
                       data.get("revisit_after"), data.get("permanent", 0)))
    out = get(place_id) or {}
    if blocked:
        out["stage_blocked"] = blocked
    return out


def delete(place_id: str, actor: str = "user") -> bool:
    c = conn()
    with c:
        cur = c.execute("SELECT 1 FROM sites WHERE place_id=?", (place_id,)).fetchone()
        if not cur:
            return False
        # Remove the children too; these tables carry no FK, so nothing else
        # would ever clean them up.
        c.execute("DELETE FROM sites WHERE place_id=?", (place_id,))
        c.execute("DELETE FROM decisions WHERE place_id=?", (place_id,))
        c.execute("DELETE FROM campaign_members WHERE place_id=?", (place_id,))
        _log(c, place_id, actor, "deleted")   # the event log is kept deliberately
    return True


# ── campaigns ──────────────────────────────────────────────────────────────
def create_campaign(name: str, place_ids: list, area_note: str = "",
                    cost_cents: int | None = None, actor: str = "user") -> dict:
    cid = "camp_" + hashlib.sha256(f"{name}{_now()}".encode()).hexdigest()[:10]
    c = conn()
    with c:
        c.execute("INSERT INTO campaigns (campaign_id,name,area_note,piece_count,"
                  "cost_cents,status,created_at) VALUES (?,?,?,?,?,'draft',?)",
                  (cid, name, area_note, len(place_ids), cost_cents, _now()))
        for pid in place_ids:
            c.execute("INSERT OR IGNORE INTO campaign_members (campaign_id,place_id,added_at)"
                      " VALUES (?,?,?)", (cid, pid, _now()))
        _log(c, None, actor, "campaign_created", note=f"{name}: {len(place_ids)} sites")
    return campaign(cid)


def campaign(cid: str):
    c = conn()
    row = c.execute("SELECT * FROM campaigns WHERE campaign_id=?", (cid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["members"] = c.execute("SELECT COUNT(*) n FROM campaign_members WHERE campaign_id=?",
                             (cid,)).fetchone()["n"]
    d["responded"] = c.execute("SELECT COUNT(*) n FROM campaign_members"
                               " WHERE campaign_id=? AND responded_at IS NOT NULL",
                               (cid,)).fetchone()["n"]
    return d


def campaigns() -> list:
    return [campaign(r["campaign_id"]) for r in
            conn().execute("SELECT campaign_id FROM campaigns ORDER BY created_at DESC")]


def transition_campaign(cid: str, to_stage: str, effective: str | None = None,
                        vendor_ref: str | None = None, actor: str = "user") -> dict:
    """Advance every member of a campaign in ONE transaction.

    Two rules this enforces:
      * members with no site row are reported, never created — a stale id in a
        2,000-row campaign used to silently become a nameless site;
      * members already further along are skipped, not moved backwards.
    """
    c = conn()
    eff = effective or date.today().isoformat()
    rank = STAGE_RANK.get(to_stage, 0)
    now = _now()
    with c:                                   # one transaction for the whole batch
        members = [r["place_id"] for r in c.execute(
            "SELECT place_id FROM campaign_members WHERE campaign_id=?", (cid,))]
        known, advance = {}, []
        for i in range(0, len(members), 400):          # chunked: SQLite caps variables
            chunk = members[i:i + 400]
            q = ",".join("?" * len(chunk))
            for r in c.execute(f"SELECT place_id, stage FROM sites WHERE place_id IN ({q})", chunk):
                known[r["place_id"]] = r["stage"]
        unknown = [m for m in members if m not in known]
        for pid, cur_stage in known.items():
            if rank > STAGE_RANK.get(cur_stage or "", 0):
                advance.append(pid)

        for i in range(0, len(advance), 400):
            chunk = advance[i:i + 400]
            q = ",".join("?" * len(chunk))
            c.execute(f"UPDATE sites SET stage=?, status='pipeline', updated=?, updated_by=?"
                      f" WHERE place_id IN ({q})", [to_stage, now, actor] + chunk)
        c.executemany("UPDATE decisions SET superseded=1 WHERE place_id=? AND superseded=0",
                      [(p,) for p in advance])
        c.executemany("INSERT INTO decisions (place_id,status,stage,decided_at,decided_by)"
                      " VALUES (?,?,?,?,?)",
                      [(p, "pipeline", to_stage, now, actor) for p in advance])
        c.executemany("INSERT INTO events (place_id,ts,actor,kind,field,new_value,note)"
                      " VALUES (?,?,?,?,?,?,?)",
                      [(p, now, actor, "campaign_stage", "stage", to_stage, cid) for p in advance])

        c.execute("UPDATE campaigns SET status=?, mailed_at=COALESCE(mailed_at,?),"
                  " vendor_ref=COALESCE(?,vendor_ref) WHERE campaign_id=?",
                  ("mailed" if to_stage == "postcard_mailed" else "active",
                   eff if to_stage == "postcard_mailed" else None, vendor_ref, cid))
        if to_stage == "postcard_mailed":
            c.execute("UPDATE campaign_members SET mailed_at=? WHERE campaign_id=?", (eff, cid))
        _log(c, None, actor, "campaign_transition",
             note=f"{cid} -> {to_stage}: {len(advance)} advanced, "
                  f"{len(known)-len(advance)} skipped, {len(unknown)} unknown")
    return {"campaign_id": cid, "to": to_stage, "advanced": len(advance),
            "skipped": len(known) - len(advance), "unknown": unknown,
            "members": len(members)}

def backup(dest: str | None = None) -> str:
    """Consistent snapshot via VACUUM INTO — safe while the server is running.
    sites.db is the only copy of every decision and is gitignored."""
    out = Path(dest) if dest else ROOT / f"backups/sites-{datetime.now():%Y%m%d-%H%M%S}.db"
    out.parent.mkdir(parents=True, exist_ok=True)
    conn().execute("VACUUM INTO ?", (str(out),))
    return str(out)
