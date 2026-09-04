# ATM Map validation record

## Recorded run

| Field | Value |
|---|---|
| Run date | 2026-09-04 |
| Runtime | Python 3.12.3 |
| Core result | 80 tests passed; the server-dependent HTTP class was skipped while the service was stopped |
| Active-service result | 83 tests passed, including all three HTTP boundary checks |
| Paid provider calls | none; the active-service check used placeholder keys and exercised only local/static routes |

## Commands

Core suite:

```bash
python3 -m unittest discover -s tests -v
```

HTTP boundary included:

```bash
GOOGLE_MAPS_API_KEY=test MAPS_BROWSER_KEY=test python3 proxy.py
# In a second shell:
python3 -m unittest discover -s tests -v
```

The active-service run confirmed that public application assets load while the
SQLite database, environment file, source files, legacy data source, and path
traversal requests return `403`.

A protected-host smoke check also confirmed that the fixed, data-free
`/healthz` response is available without credentials while the application root
continues to return `401` without credentials.

## Browser workflow

`tests/browser_search_history.py` ran the application in Chromium against an
isolated temporary database and deterministic provider stub. It verified a new
search ID, persisted results, History listing and filtering, mouse and keyboard
row opening, exact-result reopen, direct links, formula-safe CSV export,
budget-stopped and interrupted states, immediate display of checkpointed
results after a later category fails, and a linked cached rerun with zero
simulated provider calls. No paid API was used.

## Completed market run

| Measure | Result |
|---|---:|
| Planned / completed areas | 25 / 21 |
| Completed among areas started | 21 / 21 |
| Isolated ledger / configured cap | 224 / 225 calls |
| Application ledger / operator-recorded Cloud requests | 224 / 224 |
| Candidate observations / unique Place IDs | 1,961 / 1,856 |
| Cross-area repeat observations | 105 |
| Gross completed-circle area | 412.3 km² |
| Median / p95 area runtime | 4.302 s / 8.16 s |
| Warm-cache digest controls | 4 / 4 matched at 0 new calls |
| Manual category review | 48 / 50 plausible |

The call guard stopped before starting four additional areas, leaving one call
unused. All 21 started areas completed without application-reported truncation.
Google Cloud's aggregate dashboard recorded eight unclassified error events
during the same period. The application completed the attempted work, but the
aggregate console view does not independently establish upstream success for
every request; status-code classification is the next monitoring refinement.

The complete public aggregate and artifact hashes are recorded in
[`benchmarking/evidence/MARKET_RUN_20260904.md`](benchmarking/evidence/MARKET_RUN_20260904.md).

## Coverage by operating risk

| Risk or rule | Test area |
|---|---|
| Dense search exceeds the approved call budget | `AdaptiveTiling` |
| Repeated search repurchases cached coverage | `AdaptiveTiling`, `HardLifetimeCap` |
| The response field mask is costed against the wrong Places SKU or free-usage cap | `PricingAccounting` |
| Missing provider data becomes a false negative | `ShopAndAtmDetection` |
| Co-located or adjacent businesses are merged incorrectly | `CoLocationMerging`, `LocationIdentity` |
| Historical ATM identity is lost or repeatedly re-resolved | `AtmResolution`, `PlaceMetadata` |
| A reviewed candidate cannot move from Verify first to Ready to mail | `ManualVerification` |
| Search results disappear after closing the browser or restarting the service | `SearchRunHistory`, `DurableSearchRuns`, and the Chromium history flow |
| A later provider failure discards usable results from completed categories | `DurableSearchRuns.test_provider_failure_returns_checkpointed_partial_results` and the Chromium history flow |
| Bulk or stale state moves the sales funnel backward | `MonotonicFunnel`, `CampaignTransition` |
| Rejections return at the wrong time | `RevisitMaths` |
| CRM credentials, vocabulary mapping, or outbound failure breaks the local workflow | `CrmAuthentication`, `CrmVocabulary`, `CrmFailureBoundary` |
| Formula-like provider values execute when a CSV is opened | Chromium history/export flow |
| A public route exposes data, credentials, or source | `StaticFileAccess` |

This record validates implemented controls. Professional adoption and operating
results remain a separate evidence class in `BUSINESS_CASE.md`.
