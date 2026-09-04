# ATM Map — discovery, delivery, and launch readiness

This record connects stakeholder needs to user stories, delivery decisions,
handoffs, risks, and release gates for the prospecting workflow.

## Discovery synthesis

Requirements were derived from the manual list-building workflow, feedback from
the company owner and business-development users, and the downstream mailing
and pipeline actions the candidate data needed to support.

| Stakeholder or workflow | Observed need | Constraint or failure mode | Requirement derived | Validation |
|---|---|---|---|---|
| Business-development user | build a qualified list across a market | one-at-a-time lookups hid coverage, omissions, duplicates, and prior review | search a bounded area, preserve coverage, and carry each candidate into a clear next action | adopted operating workflow and live application |
| Company owner | scale prospecting without uncontrolled external cost | automation could replace a labor constraint with an API-spend constraint | show usage before a run, require explicit start, reuse cache, enforce caps and a kill switch | live status controls and automated budget tests |
| Candidate reviewer | decide whether a location is actionable | provider metadata is incomplete and nearby tenants can be confused | preserve unknowns, retain evidence, merge conservatively, and allow explicit manual approval | qualification, co-location, and manual-verification tests |
| Mailing workflow | act on thousands of records consistently | spreadsheet handoffs can duplicate records or overwrite later funnel state | govern export, model campaigns as cohorts, and apply monotonic bulk transitions | campaign and funnel tests |
| Sales/CRM workflow | retain owner, stage, history, and next action | system vocabularies and field ownership can conflict | define a canonical stage vocabulary and route integration writes through the same business rules | API boundary and configurable CRM adapter |

## User stories and acceptance

| ID | User story | Acceptance condition | Delivery state |
|---|---|---|---|
| US-01 | As a BD user, I want to search a ZIP, radius, or drawn market so I can build coverage without opening listings one by one. | Search stays inside the requested geometry, subdivides dense areas within budget, and maps returned candidates. | Implemented and technically validated |
| US-02 | As a BD user, I want existing locations, pipeline prospects, and new candidates on one map so I can avoid duplicate outreach and see coverage gaps. | Each source has a visible layer and a known record is distinguishable before a new decision. | Implemented and used in the operating workflow |
| US-03 | As a reviewer, I want missing evidence shown as unknown so I do not reject a viable business because a provider omitted a field. | Missing shop, ATM, or hours data routes to `Verify first`, never a definitive negative. | Implemented and technically validated |
| US-04 | As a reviewer, I want to approve a `Verify first` candidate after manual review so it can enter the mailing workflow. | Manual approval moves the candidate to `Ready to mail`, is visible as human evidence, and reverses cleanly. | Implemented and technically validated |
| US-05 | As an owner, I want to see and bound paid usage before and during a search so scale remains economically controlled. | The UI forecasts calls/cost; cache, burst, daily, lifetime, and kill-switch controls refuse excess use. | Implemented and technically validated |
| US-06 | As a campaign operator, I want bulk actions to preserve later sales progress so stale files cannot move a prospect backward. | Eligible records advance transactionally; later-stage, unknown, and protected records are reported rather than overwritten. | Implemented and technically validated |
| US-07 | As a sales user, I want rejected prospects to return only when the reason supports a revisit. | Revisit date and permanence follow the reason; do-not-contact remains excluded. | Implemented and technically validated |
| US-08 | As a BD user, I want to retrieve a past market search so I can resume review, share the exact result set, or rerun it without reconstructing the area. | Every run receives a short ID; History filters, reopens, links, exports, and creates a new linked rerun while preserving the original snapshot. | Implemented and browser-validated |
| US-09 | As a BD user, I want completed search work to remain usable if a later provider request fails. | The interface shows checkpointed candidates, identifies the failed category, and preserves the run for review, sharing, export, or rerun. | Implemented and browser-validated |

## Delivery decisions

| Decision | Business reason | Implementation |
|---|---|---|
| Use Google Places as the operating search source | the production workflow requires current commercial listings and bounded geographic search | restricted Places requests, place identity, evidence timestamps, and metered usage |
| Separate physical location from business occupant | tenants can change while a useful site address remains | `locations` and `sites` remain separate; history stays with the applicable business |
| Preserve `unknown` as a first-class state | missing provider fields are not evidence that a feature is absent | tri-state qualification and `Verify first` review |
| Keep human decisions separate from provider evidence | refreshed listing data must not silently undo a reviewed decision | explicit manual verification, append-only decisions, and audit events |
| Use adaptive search rather than a fixed grid | dense areas need more resolution while sparse areas should not pay for empty tiles | saturation-based subdivision within a request budget |
| Make the application API the write boundary | direct database writes can violate funnel, history, and do-not-contact rules | UI, bulk actions, and integrations pass through shared store rules |
| Persist searches as durable runs | browser-only results cannot support handoff, recovery, or repeat analysis | allocate an ID before provider work, checkpoint progress, return completed-category results on later failure, retain terminal states, and link identical reruns |

## Roles and handoffs

| Role | Owns | Handoff |
|---|---|---|
| Company owner | target markets, campaign economics, spend ceiling, and final operating policy | approves market scope, budget, and readiness for a mailing cohort |
| Business-development user | candidate review, manual verification, decision reason, owner, and next action | moves an approved candidate to campaign, contact, revisit, or rejection |
| Campaign or mailing operator | cohort, piece count, vendor reference, mailed date, and response attribution | returns campaign and response state to the operating record |
| CRM owner | canonical sales owner and stage after CRM activation | confirms field ownership and vocabulary mapping before synchronization is enabled |
| Technical owner | search provider, data integrity, access, cost controls, backup, recovery, and monitoring | resolves defects and releases only after technical gates pass |

## Risk, dependency, and open-decision register

| Item | Owner | Control or decision | State |
|---|---|---|---|
| Google availability, quota, or price changes | owner + technical owner | pre-run forecast, explicit run, cache, caps, kill switch, and cost-model sensitivity | controlled and monitored |
| Provider data is missing or stale | BD user | preserve unknowns, manual verification, evidence timestamps, and stale-site review | controlled; stale-site flag is the next enhancement |
| Co-located businesses are merged incorrectly | technical owner + reviewer | require address and proximity evidence; retain tenant names | automated regression coverage |
| Bulk campaign state overwrites later sales progress | technical owner | monotonic stages, transactional cohort updates, and exception report | automated regression coverage |
| Spreadsheet export executes formula-like values | technical owner | escape formula control characters during CSV serialization | implemented control |
| CRM and map disagree about field ownership | CRM owner + technical owner | agree the ownership matrix and vocabulary before enabling synchronization | adapter remains disabled pending that decision |
| Mailing responses cannot be attributed to a cohort | campaign owner | use campaign- or market-specific QR/landing codes and retain campaign membership | defined for the next closed cohort |
| Live review consumes unbounded API spend | owner + technical owner | password protection, restricted keys, lifetime cap, and deployment shutdown procedure | controlled for the review deployment |
| Search work is lost after browser or process interruption | technical owner | durable run IDs, transactional checkpoints, startup recovery, and exact snapshots | implemented and regression-tested |
| A provider fails after paid search work completes | technical owner | return and persist completed-category candidates, identify unfinished categories, and retain a linked rerun path | implemented and browser-tested |

## Launch-readiness checklist

| Gate | Evidence | State |
|---|---|---|
| Business workflow and primary users are defined | `BUSINESS_CASE.md` and discovery synthesis above | complete |
| Core user stories have testable acceptance conditions | user stories above and `DATA_HUB.md` operational criteria | complete |
| Data ownership, lineage, and integrity rules are defined | `DATA_ARCHITECTURE.md` | complete |
| Search, cost, qualification, state, campaign, revisit, CRM-boundary, and recovery controls pass regression | 80 stopped-service checks, 83 active-service checks, and the Chromium Search History/formula-safe export and partial-recovery flow | complete |
| Bounded market behavior is measured at realistic volume | 21 completed areas, 1,856 unique returned primary Place IDs, 224 application-ledger calls matched to the operator-recorded Cloud count, cache controls, and a manual category review | complete |
| Protected live-review deployment has access, spend, recovery, and shutdown controls | `DEPLOY.md` | complete for the review deployment |
| CRM field ownership and authentication are approved | CRM owner decision | required before enabling synchronization |
| Closed-campaign outcome attribution is configured | campaign identifiers and response capture | required for the next outcome cohort |

The workflow can operate without CRM synchronization, so that integration is a
controlled follow-on rather than a hidden dependency of candidate production or
mailing.
