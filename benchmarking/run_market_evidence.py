#!/usr/bin/env python3
"""Run a bounded, reproducible market-search evidence exercise.

Safe by default: without both --execute and the exact --confirm-live-calls
value, this command prints the pre-registered plan and makes no network calls.
Live outputs are written outside the repository unless the operator explicitly
chooses another directory. Aggregate evidence is separated from the local-only
candidate audit sample.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PROXY_PATH = ROOT / "proxy.py"
DEFAULT_ENV_FILE = ROOT / ".env"
DEFAULT_OUTPUT_ROOT = Path.home() / "atm-market-evidence-runs"
SEGMENTS = ["smoke", "liquor", "gas", "laundromat", "convenience"]
WARM_CELL_IDS = {"r1c1", "r1c5", "r3c3", "r5c1", "r5c5"}


def grid_cells(center_lat: float, center_lng: float, rows: int = 5,
               cols: int = 5, spacing_km: float = 4.5,
               radius_m: float = 2500.0) -> list[dict]:
    """Return the fixed row-major grid used by the evidence protocol."""
    lat_step = spacing_km / 111.32
    lng_step = spacing_km / (111.32 * math.cos(math.radians(center_lat)))
    cells = []
    for row in range(rows):
        for col in range(cols):
            cells.append({
                "id": f"r{row + 1}c{col + 1}",
                "lat": round(center_lat + (row - (rows - 1) / 2) * lat_step, 6),
                "lng": round(center_lng + (col - (cols - 1) / 2) * lng_step, 6),
                "radius_m": radius_m,
            })
    return cells


def load_api_key(env_file: Path) -> None:
    if os.getenv("GOOGLE_MAPS_API_KEY"):
        return
    if not env_file.exists():
        raise RuntimeError(f"No GOOGLE_MAPS_API_KEY and no env file at {env_file}")
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "GOOGLE_MAPS_API_KEY" and value.strip():
            os.environ["GOOGLE_MAPS_API_KEY"] = value.strip()
            return
    raise RuntimeError(f"GOOGLE_MAPS_API_KEY was not found in {env_file}")


def load_proxy():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("atm_evidence_proxy", PROXY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import {PROXY_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command_output(*args: str) -> str:
    return subprocess.run(args, check=True, text=True,
                          capture_output=True).stdout.strip()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ids_digest(rows: list[dict]) -> str:
    ids = sorted(str(row.get("place_id") or "") for row in rows)
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def distribution(rows: list[dict], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(field)) for row in rows).items()))


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[max(0, index)], 3)


def summarize_cell(cell: dict, result: dict, elapsed_s: float) -> dict:
    rows = result.get("results") or []
    truncated = result.get("truncated_segments") or []
    error = result.get("error")
    refused = result.get("refused")
    return {
        **cell,
        "segments": SEGMENTS,
        "elapsed_s": round(elapsed_s, 3),
        "candidate_count": len(rows),
        "candidate_id_digest_sha256": ids_digest(rows),
        "api_calls": result.get("api_calls", 0),
        "cache_hits": result.get("cache_hits", 0),
        "tiles": result.get("tiles", 0),
        "merged_duplicates": result.get("merged_duplicates", 0),
        "truncated_segments": truncated,
        "coverage": result.get("coverage") or {},
        "error": error,
        "refused": refused,
        "complete": not error and not refused and not truncated,
        "hours_status": distribution(rows, "hours_ok"),
        "shop_status": distribution(rows, "has_shop"),
        "possible_competitor_status": distribution(rows, "btc_competitor"),
    }


def write_audit_sample(path: Path, rows: list[dict], limit: int = 50) -> None:
    """Write a deterministic local-only sample for human listing review."""
    deduped = {}
    for row in rows:
        place_id = str(row.get("place_id") or "")
        if place_id:
            deduped.setdefault(place_id, row)
    chosen = sorted(
        deduped.values(),
        key=lambda row: hashlib.sha256(str(row["place_id"]).encode()).hexdigest(),
    )[:limit]
    fields = ["place_id", "name", "address", "lat", "lng", "seg", "hours",
              "hours_ok", "has_shop", "btc_competitor", "audit_correct",
              "audit_issue", "auditor_note"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in chosen:
            writer.writerow({**{key: row.get(key, "") for key in fields},
                             "audit_correct": "", "audit_issue": "",
                             "auditor_note": ""})


def markdown_summary(data: dict) -> str:
    aggregate = data["aggregate"]
    lines = [
        "# ATM market-search evidence summary",
        "",
        f"Run: `{data['run_id']}`  ",
        f"Started: {data['started_at']}  ",
        f"Finished: {data['finished_at']}  ",
        "Evidence class: current implementation validation; this is not a causal business-outcome study.",
        "",
        "## Result",
        "",
        f"- Paid Text Search calls recorded by the isolated ledger: **{aggregate['api_calls']} / {data['controls']['max_live_calls']}**.",
        f"- Grid cells completed without reported truncation: **{aggregate['complete_cells']} / {aggregate['attempted_cells']}**.",
        f"- Candidate observations across completed cells: **{aggregate['candidate_observations']}**.",
        f"- Unique candidate IDs across completed cells: **{aggregate['unique_candidates']}**.",
        f"- Cross-cell repeat observations removed in the aggregate: **{aggregate['cross_cell_repeats']}**.",
        f"- Gross searched-circle area (overlap not deducted): **{aggregate['gross_circle_area_km2']} km²**.",
        f"- Throughput: **{aggregate['unique_candidates_per_call']} unique candidates/call** and **{aggregate['calls_per_gross_km2']} calls/gross km²**.",
        f"- Cell timing: median **{aggregate['cell_latency_median_s']} s**, p95 **{aggregate['cell_latency_p95_s']} s**.",
        "",
        "## Cell record",
        "",
        "| Cell | Candidates | Calls | Tiles | Complete | Seconds |",
        "|---|---:|---:|---:|---|---:|",
    ]
    for cell in data["cells"]:
        lines.append(
            f"| {cell['id']} | {cell['candidate_count']} | {cell['api_calls']} | "
            f"{cell['tiles']} | {'Yes' if cell['complete'] else 'No'} | "
            f"{cell['elapsed_s']} |")
    lines.extend(["", "## Warm-cache controls", "",
                  "| Cell | Same candidate digest | Zero new calls |",
                  "|---|---|---|"])
    for check in data["warm_cache_checks"]:
        lines.append(
            f"| {check['cell_id']} | {'Yes' if check['same_digest'] else 'No'} | "
            f"{'Yes' if check['zero_api_calls'] else 'No'} |")
    lines.extend([
        "",
        "## Interpretation boundary",
        "",
        "- Only complete cells are included in aggregate candidate and area metrics.",
        "- No geocoding, photos, saved-site mutation, pipeline writes, or raw provider payload retention occurred.",
        "- The local candidate audit CSV contains listing detail for human review and should not be committed.",
        "- Reconcile the isolated call count with Google Cloud Billing before publishing cost evidence; free usage is shared at billing-account level.",
        "- List-price exposure is shown for comparability; the actual invoice can differ because of free usage and account-wide volume tiers.",
        "",
    ])
    return "\n".join(lines)


def plan_payload(args, cells: list[dict]) -> dict:
    footprint_side_km = (5 - 1) * args.spacing_km + 2 * args.radius_m / 1000
    return {
        "network_calls": False,
        "execute_requires": f"--execute --confirm-live-calls {args.max_calls}",
        "max_live_calls": args.max_calls,
        "pricing": "Text Search Enterprise; $35/1,000; 1,000 free/month as of 2026-09-03",
        "grid": {
            "cells": len(cells), "rows": 5, "columns": 5,
            "center": [args.center_lat, args.center_lng],
            "spacing_km": args.spacing_km, "radius_m": args.radius_m,
            "nominal_bounding_footprint_km": round(footprint_side_km, 1),
            "segments_per_cell": SEGMENTS,
        },
        "outputs": ["aggregate results JSON", "Markdown summary",
                    "local-only 50-record audit CSV", "file hashes"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true",
                        help="make live Places requests")
    parser.add_argument("--confirm-live-calls", type=int)
    parser.add_argument("--max-calls", type=int, default=225)
    parser.add_argument("--center-lat", type=float, default=34.0522)
    parser.add_argument("--center-lng", type=float, default=-118.2437)
    parser.add_argument("--spacing-km", type=float, default=4.5)
    parser.add_argument("--radius-m", type=float, default=2500.0)
    parser.add_argument("--pause-between-cells", type=float, default=1.0)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    if args.max_calls <= 0 or args.max_calls > 1000:
        parser.error("--max-calls must be between 1 and 1000")

    cells = grid_cells(args.center_lat, args.center_lng,
                       spacing_km=args.spacing_km, radius_m=args.radius_m)
    if not args.execute:
        print(json.dumps(plan_payload(args, cells), indent=2))
        return 0
    if args.confirm_live_calls != args.max_calls:
        parser.error("live execution requires an exact --confirm-live-calls value")

    status = command_output("git", "-C", str(ROOT), "status", "--short")
    if status and not args.allow_dirty:
        raise RuntimeError("Refusing an evidence run from a dirty worktree")

    load_api_key(args.env_file)
    proxy = load_proxy()
    now = datetime.now(ZoneInfo("America/New_York"))
    run_id = now.strftime("run-%Y%m%d-%H%M%S-et")
    run_dir = args.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    proxy.CACHE_DIR = run_dir / "isolated-cache"
    proxy.USAGE_FILE = run_dir / "isolated-usage.json"
    proxy.KILL_FILE = run_dir / "isolated-killswitch"
    proxy.PHOTO_DIR = run_dir / "unused-photo-cache"
    proxy.DAILY_CALL_CAP = args.max_calls
    proxy.HARD_CALL_CAP = args.max_calls
    proxy.BURST_MAX = args.max_calls
    proxy.MONTHLY_BUDGET_USD = 100.0
    proxy.load_sites = lambda: {}

    data = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": now.isoformat(),
        "source_commit": command_output("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        "source_files_sha256": {
            "proxy.py": file_sha256(PROXY_PATH),
            "tests/test_search.py": file_sha256(ROOT / "tests" / "test_search.py"),
            "benchmarking/run_market_evidence.py": file_sha256(Path(__file__)),
        },
        "controls": {
            "max_live_calls": args.max_calls,
            "isolated_cache": True,
            "isolated_usage_ledger": True,
            "isolated_killswitch": True,
            "saved_sites_excluded": True,
            "geocoding": False,
            "photos": False,
            "pipeline_writes": False,
            "pricing_as_of": proxy.PRICING_AS_OF,
            "search_sku": proxy.SKU["search"],
        },
        "grid": plan_payload(args, cells)["grid"],
        "cells": [],
        "warm_cache_checks": [],
    }

    complete_rows = []
    rows_by_cell = {}
    minimum_cell_calls = len(SEGMENTS)
    for cell in cells:
        remaining = args.max_calls - proxy.status()["calls_today"]
        if remaining < minimum_cell_calls:
            break
        proxy.MAX_CALLS_PER_SEARCH = min(40, remaining)
        print(f"cold {cell['id']} ({remaining} calls remain)", flush=True)
        started = time.perf_counter()
        result = proxy.search(cell["lat"], cell["lng"], cell["radius_m"], SEGMENTS)
        elapsed = time.perf_counter() - started
        summary = summarize_cell(cell, result, elapsed)
        data["cells"].append(summary)
        rows_by_cell[cell["id"]] = result.get("results") or []
        if summary["complete"]:
            complete_rows.extend(rows_by_cell[cell["id"]])
        if result.get("refused") or result.get("error"):
            break
        time.sleep(max(0.0, args.pause_between_cells))

    for cell in cells:
        if cell["id"] not in WARM_CELL_IDS or cell["id"] not in rows_by_cell:
            continue
        cold_rows = rows_by_cell[cell["id"]]
        result = proxy.search(cell["lat"], cell["lng"], cell["radius_m"], SEGMENTS)
        warm_rows = result.get("results") or []
        data["warm_cache_checks"].append({
            "cell_id": cell["id"],
            "same_digest": ids_digest(cold_rows) == ids_digest(warm_rows),
            "zero_api_calls": result.get("api_calls") == 0,
            "reported_cache_hits": result.get("cache_hits", 0),
        })

    unique_ids = {str(row.get("place_id")) for row in complete_rows if row.get("place_id")}
    complete_cells = [cell for cell in data["cells"] if cell["complete"]]
    calls = proxy.status()["calls_today"]
    gross_area = len(complete_cells) * math.pi * (args.radius_m / 1000) ** 2
    latencies = [cell["elapsed_s"] for cell in complete_cells]
    data["aggregate"] = {
        "attempted_cells": len(data["cells"]),
        "complete_cells": len(complete_cells),
        "api_calls": calls,
        "candidate_observations": len(complete_rows),
        "unique_candidates": len(unique_ids),
        "cross_cell_repeats": len(complete_rows) - len(unique_ids),
        "gross_circle_area_km2": round(gross_area, 1),
        "unique_candidates_per_call": round(len(unique_ids) / calls, 2) if calls else None,
        "calls_per_gross_km2": round(calls / gross_area, 3) if gross_area else None,
        "cell_latency_median_s": round(statistics.median(latencies), 3) if latencies else None,
        "cell_latency_p95_s": percentile(latencies, 0.95),
        "list_price_exposure_usd": round(calls * proxy.SKU["search"]["per_1k"] / 1000, 2),
        "hours_status": distribution(complete_rows, "hours_ok"),
        "shop_status": distribution(complete_rows, "has_shop"),
        "possible_competitor_status": distribution(complete_rows, "btc_competitor"),
    }
    data["finished_at"] = datetime.now(ZoneInfo("America/New_York")).isoformat()

    results_path = run_dir / "benchmark-results.json"
    summary_path = run_dir / "benchmark-summary.md"
    audit_path = run_dir / "manual-audit-local-only.csv"
    results_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    summary_path.write_text(markdown_summary(data), encoding="utf-8")
    write_audit_sample(audit_path, complete_rows)
    hashes = {
        path.name: file_sha256(path)
        for path in (results_path, summary_path, audit_path)
    }
    (run_dir / "artifact-hashes.sha256.json").write_text(
        json.dumps(hashes, indent=2), encoding="utf-8")
    print(f"summary: {summary_path}")
    print(f"results: {results_path}")
    print(f"local audit sample: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
