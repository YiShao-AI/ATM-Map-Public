# Bounded market-search evidence run

This protocol turns a limited Google Places budget into auditable evidence of
coverage behavior, cache reuse, throughput, completeness, and data quality. It
does not change application or pipeline data, and it does not claim that a
technical run proves sales outcomes.

## Why 225 calls

The public deployment defaults to a 250-call daily ceiling. A 225-call evidence
budget leaves 25 calls of operational headroom. At the current Text Search
Enterprise list rate, its maximum list-price exposure is `$7.88`; the invoice
may be lower when billing-account free usage remains. Confirm both quota and
billing state in Google Cloud before executing.

The earlier bounded validation used 7–9 calls for five categories in a 2.5 km
cell. A 225-call cap should therefore attempt roughly 25 cells under similar
density, but the runner never assumes that estimate: it records incomplete
cells and excludes them from area and candidate aggregates.

## Pre-registered design

- 5 × 5 row-major grid centered on Downtown Los Angeles;
- 4.5 km between centers and a 2.5 km radius per cell;
- the same five segments in every cell;
- cold isolated cache and usage ledger;
- a hard ceiling of 225 live Text Search calls;
- five fixed warm repeats (four corners and center);
- no geocoding, photos, saved-site data, pipeline mutation, or raw API payloads;
- aggregate JSON and Markdown evidence, plus a local-only 50-record audit CSV.

The gross area metric sums completed search circles and explicitly does not
deduct overlap. The unique-candidate metric deduplicates Place IDs across cells.
Both are reported so a reviewer can see the cost of overlap rather than receive
an inflated coverage claim.

## Completed run

The bounded run was completed on 2026-09-04. It used 224 of 225 permitted calls,
completed every one of the 21 areas it started, reconciled 1,961 observations to
1,856 unique Place IDs, and reproduced four pre-selected areas from cache with
zero new provider calls.

- Human-readable record: [`evidence/MARKET_RUN_20260904.md`](evidence/MARKET_RUN_20260904.md)
- Machine-readable aggregate: [`evidence/market-run-20260904.json`](evidence/market-run-20260904.json)
- Source-provenance check: `python3 benchmarking/verify_market_run_source.py`

## Safe preview

This makes no network calls:

```bash
python3 benchmarking/run_market_evidence.py
```

## Intentional live run

Run only on the protected laptop after pulling a clean commit and confirming
the Google Cloud billing-account usage:

```bash
python3 benchmarking/run_market_evidence.py \
  --execute \
  --confirm-live-calls 225 \
  --env-file /path/to/private/.env
```

The output defaults to `~/atm-market-evidence-runs`, outside this repository.
The command refuses a dirty worktree unless `--allow-dirty` is explicitly used.
Do not use that override for evidence intended for review.

## Evidence completion checklist

1. Save a before/after Google Cloud usage screenshot for the applicable SKU.
2. Confirm the isolated ledger and Cloud usage delta reconcile; explain any
   concurrent billing-account traffic.
3. Review all incomplete cells before quoting area or throughput.
4. Complete the 50-row local audit and summarize issue counts by category.
5. Confirm all five warm repeats return the same candidate digest with zero new
   calls.
6. Retain the source commit, hashes, exact grid, timings, and machine/runtime
   notes with the run.
7. Publish only aggregate evidence. Keep the candidate-level audit file local.

## Stop conditions

Stop and retain the partial record if the provider rejects a request, a spend
guard activates, a cell reports unexplained truncation, or Cloud usage diverges
materially from the isolated ledger. A partial, explained run is stronger
evidence than a silently edited result.
