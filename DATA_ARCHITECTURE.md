# ATM Map data architecture and ownership

This document describes the implemented data model, the source of each major
field, the controls that protect workflow state, and the integration boundary
for a CRM or mailing system.

## Architecture decision

The application uses SQLite as its transactional store and exposes business
operations through the application API. Other systems should integrate through
that API rather than write directly to database tables.

That boundary matters because a syntactically valid database update can still
violate an operating rule. Examples include moving a signed site back to
`postcard_mailed`, clearing a do-not-contact instruction, or converting missing
provider metadata into a false negative. Centralizing writes allows those rules
to be enforced consistently and recorded in the audit log.

```text
Google Places + Geocoding ──► search / enrichment service ──► browser map
                                        │                         │
historical ATM export ──────────────────┤                         │
                                        ▼                         ▼
                             SQLite operating store ◄──── application API
                                        │
                         CRM webhook / mailing workflow / exports
```

## Implemented entities

| Entity | Purpose | Stable key |
|---|---|---|
| `locations` | Physical address and coordinates that survive a tenant change | normalized address + rounded coordinates hash |
| `sites` | Business occupying a location, plus current workflow state | Google `place_id` |
| `place_meta` | Search and enrichment evidence shared by candidates and existing ATMs | Google `place_id` |
| `atm_link` | Permanent link from a decommissioned machine record to a mapped place | ATM serial number |
| `decisions` | Append-only status history, including rejection reason and revisit rule | generated `decision_id` |
| `events` | Field-level audit of changes and blocked transitions | generated event ID |
| `campaigns` | Mailing cohort, cost, status, and effective dates | generated `campaign_id` |
| `campaign_members` | Membership and response state for each site in a campaign | `campaign_id` + `place_id` |
| `search_runs` | Search parameters, lifecycle state, counters, coverage, timing, and repeat lineage | human-readable `S-XXXX-XXXX` ID |
| `search_run_results` | Ordered result snapshot for history, recovery, and export | search ID + result position |

The current schema is defined in [`store.py`](store.py). A one-time migration
path retains compatibility with the earlier JSON store without using JSON as the
live transactional source.

## Data ownership

| Data | Current owner | Integration rule |
|---|---|---|
| ATM serial number and historical installed address | decommissioned fleet export | Retain as historical operational identity; enrich but do not replace. |
| Business identity, address, coordinates, hours, rating, photos | map/search service | Store provider evidence with source and enrichment timestamp. |
| Qualification and manual verification | ATM Map user | Preserve unknowns; record human approval separately from provider evidence. |
| Decision, reason, owner, and revisit date | ATM Map workflow | Append decision history and log field changes. |
| Campaign membership and mailed date | campaign workflow | Apply cohort transitions transactionally. |
| Deal stage and sales owner after CRM activation | CRM | Mirror through the adapter; do not permit a stale update to move the funnel backward. |

The physical location and the current tenant are intentionally separate. A new
tenant may receive a new `place_id` while occupying the same address. Structural
facts can remain attached to the location, while a prior owner’s decision stays
with the earlier business.

## Data lineage

### Candidate discovery

```text
ZIP / radius / polygon
  → restricted Places request
  → adaptive subdivision where result density saturates
  → normalized candidate records
  → conservative co-location merge
  → evidence-based qualification
  → human decision or manual verification
  → SQLite decision, event, and campaign state
```

Each search receives a durable ID before provider work starts. Parameters,
progress, counters, coverage, timing, and the exact candidate snapshot are
checkpointed so the run can be reopened after the browser closes. Provider
metadata remains cached so a repeated query can reuse known evidence without
incurring an unnecessary paid lookup; the new run links back to the matching
prior query instead of overwriting it.

If a later category encounters a provider failure, the service returns and
persists candidates from completed categories, records the failed category, and
leaves the remaining categories visibly incomplete. The browser renders that
checkpoint immediately rather than replacing useful work with an empty error
state.

### Historical ATM layer

```text
serial number + decommissioned address
  → address-restricted place resolution
  → exact / near / no-match result
  → permanent `atm_link` cache
  → shared `place_meta` enrichment
  → existing-location map layer
```

The serial number and decommissioned address remain the authoritative historical
identifiers. A failed lookup is cached as well as a successful one so the same
unproductive request is not repeatedly purchased.

### CRM integration

[`crm_adapter.py`](crm_adapter.py) isolates vendor field names from the internal
stage vocabulary. Inbound updates require a shared token, prefer the
application `place_id`, fall back to an indexed `crm_id` lookup, and pass
through the same store rules as a UI action.
Outbound notification is optional and does not block the local workflow if the
CRM is unavailable.

## Integrity rules

1. **Unknown remains unknown.** Missing hours, shop type, or ATM metadata cannot
   prove the feature is absent and therefore routes the record to review.
2. **Merges require location evidence.** Co-located tenants may be grouped, but
   nearby businesses with different addresses remain separate candidates.
3. **The funnel is monotonic.** An earlier incoming stage is logged as evidence
   but does not overwrite a later stage.
4. **Decisions are historical.** A status change supersedes the current decision
   and adds a new decision row rather than erasing the earlier one.
5. **Manual verification is reversible and explicit.** A reviewed candidate can
   move from `Verify first` to `Ready to mail`; rejection automatically removes
   that approval.
6. **Revisit timing follows the rejection reason.** Temporary reasons receive a
   revisit date; permanent structural reasons do not enter the revisit queue.
7. **Bulk campaign updates are transactional.** Unknown members are reported,
   and members already further along are skipped rather than recreated or moved
   backward.
8. **Exports are safe to open in a spreadsheet.** Values beginning with formula
   control characters are prefixed before CSV serialization.
9. **Search history is immutable by rerun.** Repeating an identical query creates
   a new ID linked to the prior run; it does not replace the earlier record.
10. **Interrupted or partially failed work remains inspectable.** Segment
    checkpoints and candidate rows are transactional; provider failure returns
    completed-category candidates, and startup recovery marks unfinished work
    interrupted without deleting its saved snapshot.

## Security and access boundary

- paid API credentials stay in `.env` and are never returned by the server;
- browser and server keys have different restrictions and purposes;
- protected-demo authentication is enabled only when both credentials are set;
- `/healthz` is the sole unauthenticated route and returns only a fixed
  data-free availability response;
- runtime logs, caches, usage counters, and access records are ignored by Git;
- static-file routing refuses source, database, environment, and path-traversal
  requests; and
- API spend is bounded by explicit action, estimates, caching, burst controls,
  daily limits, a lifetime cap, and a persistent kill switch.

## Traceability

| Requirement | Implementation | Verification |
|---|---|---|
| Preserve uncertainty | qualification logic in `proxy.py` and `index.html` | `ShopAndAtmDetection` tests |
| Keep adjacent businesses distinct | co-location merge logic | `CoLocationMerging` tests |
| Retain historical ATM identity | `atm_link` and `place_meta` | `AtmResolution` and `PlaceMetadata` tests |
| Prevent funnel regression | `store.upsert` stage rank check | `MonotonicFunnel` tests |
| Retain decisions and changes | `decisions` and `events` tables | `DecisionHistory` tests |
| Support manual approval | `shortlist` state and Ready-to-mail action | `ManualVerification` tests |
| Move mailing cohorts safely | campaign transaction functions | `CampaignTransition` tests |
| Bound external spend | usage ledger, cache, hard cap, kill switch | `AdaptiveTiling` and `HardLifetimeCap` tests |
| Retain and recover market searches | `search_runs`, checkpoints, and History UI | `SearchRunHistory`, `DurableSearchRuns`, and `tests/browser_search_history.py` |

## Next integration decision

If the CRM becomes the daily system for deal progression, the recommended split
is: ATM Map owns site discovery and evidence; the CRM owns sales stage and sales
owner; the map mirrors those fields for context. Read-only analytics may use a
stable view or export, while operational writes continue through the API so the
same validation and history rules remain in force.
