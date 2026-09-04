# Completed market-search run · 2026-09-04

This record captures a bounded run of the current search implementation across
a pre-registered Los Angeles grid. It evaluates coverage, cost controls, cache
reuse, throughput, latency, and returned-data quality without writing to the
sales pipeline.

## Result

| Measure | Result |
|---|---:|
| Planned / completed areas | 25 / 21 |
| Application-level completion | 21 / 21 started areas |
| Places API calls | 224 / 225 permitted |
| Candidate observations | 1,961 |
| Unique returned primary Place IDs | 1,856 |
| Cross-area repeat observations | 105 |
| Gross completed-circle area | 412.3 km² |
| Unique returned primary Place IDs per call | 8.29 |
| Median / p95 area runtime | 4.302 s / 8.16 s |
| Maximum list-price exposure | $7.84 |

The call guard stopped before beginning the remaining four areas because fewer
than the minimum five calls remained. No started area was left incomplete and
no application-reported truncation was present in the completed cells.

## Recovery and data-quality controls

- Four pre-selected warm repeats returned identical candidate digests with zero
  new provider calls.
- Place-ID reconciliation reduced 1,961 area observations to 1,856 unique
  returned primary Place-ID records while retaining area membership for
  coverage review. Place-ID deduplication does not by itself prove that every
  record is a distinct physical site.
- A deterministic 50-record category review found 48 plausible matches and two
  category mismatches. Missing and ambiguous operating-hour fields remained
  visible for human review instead of becoming false exclusions.
- The operator-recorded Google Cloud console showed 224 Places API requests,
  matching the isolated application ledger. Its aggregate dashboard also showed
  eight unclassified error events. The application classified all 21 started
  areas as complete, but the aggregate console view does not independently
  establish upstream success for every request; status-code classification is
  retained as the next monitoring refinement.

## Qualification output

The current UI rules classified the 1,856 returned primary Place-ID records as:

| Operating state | Count | Share |
|---|---:|---:|
| Ready for outreach | 1,089 | 58.7% |
| Review needed | 456 | 24.6% |
| Not currently viable | 311 | 16.8% |

Review-needed records can be explicitly approved after a human check and moved
to Ready without changing the underlying provider evidence.

## Provenance and limited source comparability

The aggregate machine-readable record is
[`market-run-20260904.json`](market-run-20260904.json); it includes the run
design, result, qualification counts, controls, retained-artifact hashes, and
selected source fingerprints. This repository is published as a clean
snapshot, so the original private revision is not part of its history. The
recorded runner hash and selected AST fingerprints for provider requests,
pagination, adaptive tiling, accounting, normalization, and deduplication match
the inspectable public source; run
`python3 benchmarking/verify_market_run_source.py` to check them. This check
does not verify the full dependency closure, private inputs, provider responses,
or original private commit. Candidate-level exports, raw provider payloads, and
credentials are not published in this repository.
