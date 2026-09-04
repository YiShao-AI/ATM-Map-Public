# Campaign workflow and operating-state hub

The map is not only a search interface. It is the shared operating view for
existing locations, prospective sites, active pipeline records, mailing
campaigns, and revisit decisions. This document defines how that state moves
between users and systems.

## Operating objective

When thousands of candidates are in flight, status arrives through both bulk
and individual actions. The system must support each path without creating
duplicate records, losing a prior decision, or moving a prospect backward.

| Participant or source | Input | Required system behavior |
|---|---|---|
| Business-development user | qualification, decision, owner, note, stage | Save the decision with history and display the next action. |
| Mailing workflow | campaign membership and mailed date | Advance the cohort in one transaction and report exceptions. |
| Prospect response | campaign or market response | Connect the response to the applicable campaign and advance the site. |
| CRM | sales stage and sales owner | Map the external vocabulary and enforce the same funnel rules. |
| Search provider | business evidence and place identity | Enrich the record without overwriting human workflow state. |
| Existing ATM export | serial number and historical address | Preserve historical identity and resolve it to the map once. |

## End-to-end workflow

```text
1. DEFINE MARKET
   ZIP / radius / drawn area + candidate categories
        ↓
2. FORECAST AND SEARCH
   usage estimate → search ID → explicit run → checkpoints → cached evidence
        ↓
3. REVIEW
   Ready to mail / Verify first / Not viable
        ↓
4. DECIDE
   manual verification, contact, reject, do not contact, or pipeline stage
        ↓
5. ACT IN BULK
   campaign cohort → governed export → mailed transition
        ↓
6. FOLLOW THROUGH
   response → consultation → negotiation → signed → shipment → operational
        ↓
7. REVISIT OR CLOSE
   reason-specific revisit queue or permanent stop
```

## Candidate review states

| Review state | Meaning | Available action |
|---|---|---|
| `Ready to mail` | Provider evidence meets the configured operating requirements, or a user has manually verified the candidate. | Add to a campaign, export, or contact. |
| `Verify first` | One or more required facts are missing or ambiguous. | Review the listing and explicitly approve it for Ready to mail. |
| `Not viable` | Available evidence fails an operating requirement. | Reject with a reason or retain for reference. |
| Existing / pipeline | The location is already known to the fleet or sales process. | Use it to prevent duplicate outreach and understand coverage. |

Manual approval is recorded independently from provider metadata. It can be
undone, and it is automatically cleared if the site is rejected or marked do not
contact.

## Search history and retrieval

Every market run is assigned a short `S-XXXX-XXXX` ID before the first provider
call. Search History lets a user filter by ID or area, reopen the exact stored
snapshot, copy a direct link, export it, or create a new linked rerun. Completion,
partial completion, budget stop, provider failure, and process interruption are
distinct states, so operational exceptions remain visible instead of being
mistaken for an empty result.

When a provider fails after an earlier category completes, the interface
immediately displays the checkpointed candidates, names the failed category,
and shows which categories remain unfinished. The saved run can still be
reviewed, linked, exported, or rerun.

The detailed state model, endpoints, and recovery checks are in
[`SEARCH_RUNS.md`](SEARCH_RUNS.md).

## Funnel-state rules

The canonical progression is:

```text
postcard_mailed → responded / contacted → consultation → negotiating
                → contract_sent → contract_signed → shipment → operational
```

Two operating rules protect it:

1. **Advance by default.** A later stage replaces an earlier stage.
2. **Do not regress silently.** An incoming earlier stage is logged but does not
   change the current record. An explicit human decision is required to reverse
   real progress.

`rejected` and `do_not_contact` sit outside the progression. A rejection carries
a structured reason and may enter the revisit queue. Do not contact is a standing
instruction and is never cleared by a campaign transition.

## Campaign workflow

A campaign is a first-class object rather than a label copied onto thousands of
rows. It records the campaign name, area, piece count, cost, mailing date, vendor
reference, and membership.

```text
filtered candidates
  → create campaign
  → review member count
  → export mailing file
  → record mailed date and vendor reference
  → advance eligible members in one transaction
  → report advanced / skipped / unknown
  → record responses and downstream outcomes
```

The bulk transition deliberately produces an exception report:

- **advanced** — known members whose current stage is earlier than the target;
- **skipped** — known members already at the same or a later stage; and
- **unknown** — campaign identifiers with no site record, reported rather than
  silently creating blank prospects.

Campaign- or market-specific QR and landing codes can connect mailed volume,
responses, and signed contracts without requiring a unique code for every mail
piece.

## Integration boundary

The implemented API exposes sites, changed records, vocabulary, history, revisit
queue, campaigns, search, enrichment, and campaign transitions. The CRM adapter
is configuration-driven and remains inactive when no CRM credentials are set.

| Direction | Match | Behavior |
|---|---|---|
| CRM → ATM Map | application `place_id`, then indexed `crm_id` fallback | Translate stage/status, apply store rules, and record synchronization time. |
| ATM Map → CRM | linked site record | Send status/stage changes asynchronously; a CRM outage does not block local work. |
| BI or reporting → ATM Map | `updated > timestamp` | Read changed records through the incremental endpoint. |
| Mailing workflow → campaign | `campaign_id` | Apply one cohort transition and return the exception counts. |

## Exceptions and recovery

| Exception | Handling |
|---|---|
| Paid API budget is exhausted | Refuse new paid requests while preserving saved work and cached results. |
| Process stops during a search | Retain the last checkpoint and mark the run interrupted on startup. |
| Provider fails during a search | Return and preserve the run ID, completed-category candidates, failed category, unfinished categories, and error detail for review or rerun. |
| Search is repeated | Reassemble results from valid parent and child cache entries. |
| Provider field is absent | Preserve `unknown`; route the candidate to review. |
| Two businesses share an address | Merge only when address and proximity evidence support co-location; retain tenant names. |
| Bulk stage is stale | Record the event and skip the backward transition. |
| Campaign contains a stale ID | Return it in `unknown`; do not create a blank site. |
| CRM is unavailable | Keep the local update; outbound notification fails independently. |
| A site is rejected after manual approval | Clear Ready-to-mail approval and store the rejection decision. |

## Operational acceptance criteria

| ID | Acceptance criterion | Evidence |
|---|---|---|
| OPS-01 | A user can see existing, pipeline, and new candidate records in one mapped view. | Live application layers and seeded data. |
| OPS-02 | A dense search subdivides within its request budget; a sparse search does not create unnecessary child requests. | `AdaptiveTiling` tests. |
| OPS-03 | Missing evidence never appears as a definitive negative. | `ShopAndAtmDetection` tests. |
| OPS-04 | A Verify-first record can be manually approved and later reversed. | `ManualVerification` tests. |
| OPS-05 | A stale stage update cannot move the funnel backward. | `MonotonicFunnel` tests. |
| OPS-06 | A campaign transition advances eligible records atomically and reports skipped and unknown members. | `CampaignTransition` tests. |
| OPS-07 | Every status change retains decision and event history. | `DecisionHistory` tests and history endpoint. |
| OPS-08 | A permanent rejection does not enter the revisit queue. | `RevisitMaths` tests. |
| OPS-09 | Repeating an ATM place-resolution miss does not consume another paid lookup. | `PlaceMetadata` tests. |
| OPS-10 | The service refuses source files, databases, credentials, and traversal requests over HTTP. | route guard in `proxy.py`; conditional `StaticFileAccess` integration check when the local service is running. |
| OPS-11 | A completed search can be reopened by ID with the same parameters, counters, and result snapshot. | `SearchRunHistory` tests and Chromium history flow. |
| OPS-12 | Budget stop, provider failure, and process interruption remain distinguishable; a later provider failure immediately returns and retains completed-category candidates. | `DurableSearchRuns` and Chromium recovery tests. |

## Outcome reporting

The operating dashboard should close each campaign with unique candidates,
mail-ready volume, response, signed sites, productive sites, cycle time, and
total acquisition cost by source and market. Activity measures explain capacity;
signed and productive sites determine business value.
