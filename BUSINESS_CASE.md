# Business case — ATM host-site prospecting

## Executive summary

Business development built prospect lists by manual map lookups, one location at
a time. The obvious problem was how long it took. The deeper problem was not
knowing which areas had been fully covered, which viable businesses may have
been missed, which records were duplicates, or which prospects had already been
reviewed.

I turned that work into an automated prospecting process capable of surfacing and
organizing thousands of mapped candidates in a few clicks. The workflow combines
adaptive search, candidate qualification, conservative deduplication, pipeline
history, revisit logic, export, and API spending safeguards.

The operational effect was larger than faster list building. Once broad market
coverage became practical, the business could use mass mailing to generate
self-selected inbound interest instead of relying primarily on one-at-a-time cold
outreach.

## Before and after

| | Manual workflow | ATM Map workflow |
|---|---|---|
| **Scope** | Search listings one at a time | Define a ZIP, radius, or drawn market area |
| **Coverage** | Difficult to know what was already searched or missed | Search progress and mapped results make coverage visible |
| **Candidate record** | Copy fields into Excel | Return normalized, reviewable records |
| **Existing activity** | Reconcile from memory or separate files | Existing locations and pipeline prospects share the map |
| **Qualification** | Interpret each listing independently | Apply consistent evidence states while preserving unknowns |
| **Duplicates** | Reconcile after list building | Merge likely co-located results conservatively |
| **Pipeline** | Handoff to another spreadsheet | Keep decision, owner, stage, history, and revisit date attached |
| **Cost** | Labor is visible only after the work | Estimate and enforce API usage before and during a search |

The unit of work changed from “one listing copied” to “one unique candidate with
enough evidence and state for the next business-development decision.”

## The operating workflow

```text
scope a market
  → preview expected API usage and cost
  → run adaptive search
  → review candidate evidence
  → qualify and deduplicate
  → record decision, owner, stage, and revisit date
  → export or advance the campaign
  → measure closed-campaign outcomes
```

### 1. Scope and forecast

The operator chooses a ZIP, pin + radius, or drawn area and the relevant business
categories. The interface shows estimated API usage and cost before a paid search
begins. Starting the search is always an explicit action.

### 2. Search for coverage

Google Places search is restricted to the requested geometry. Dense areas are
subdivided only when result saturation indicates that a smaller search is needed;
sparse areas do not pay for unnecessary tiles. Valid cached coverage is reused on
repeat searches.

### 3. Build a usable candidate record

Provider results are normalized into a physical-site record. Existing locations,
pipeline prospects, and new candidates appear on the same map, revealing
coverage gaps and potential duplicate outreach before the next search.

### 4. Qualify without inventing certainty

Business-listing metadata is often incomplete. Missing shop, ATM, hours, or other
evidence remains unknown and routes to review; it does not become a false “no.”
Likely duplicates merge only when location evidence supports the merge, so
adjacent businesses remain separate candidates.

### 5. Preserve the funnel

The candidate record carries qualification, decision reason, owner, pipeline
stage, history, campaign membership, and revisit date. Imports and bulk actions
cannot silently erase later progress or overwrite a human decision with older
state.

## Requirements that shaped the product

| Requirement | Why it matters | Product response |
|---|---|---|
| Broad search coverage | A fast tool still fails if dense areas silently omit viable businesses | Adaptive subdivision, restricted geometry, progress, and coverage state |
| Visible spend | Automated search can convert a labor constraint into an API-cost constraint | Pre-run estimate, opt-in action, cache, daily/lifetime caps, burst control, kill switch |
| One operating view | Separate fleet, pipeline, and search files create duplicate work | Existing sites, active pipeline, and new candidates on one map |
| Honest missingness | Provider fields cannot prove a feature is absent when they are blank | Known / likely / unknown qualification states |
| Controlled deduplication | Merging too aggressively can erase viable adjacent businesses | Address and proximity evidence with conservative merge behavior |
| Durable pipeline state | Bulk imports and handoffs can corrupt history or move a deal backward | Monotonic stages, event history, reason codes, owner, and revisit logic |
| Existing downstream fit | Adoption falls when a new tool requires an entirely new operating process | Governed CSV export and campaign-ready records |

## Stakeholder adoption and strategy

The primary users were the company owner and business-development team, so the
tool had to be usable without its author present and had to fit the existing
review, spreadsheet, and mailing process.

The working system made two things visible at the same time: the scale of
candidate coverage that automation could create and the cost that scale could
introduce. That changed the stakeholder conversation from whether automation was
possible to how the business should use it.

The owner chose a mass-mail strategy that matched the new volume. Instead of BD
spending most of its time persuading cold prospects one by one, mailed recipients
could raise their hand through the contact process and enter the conversation
with existing interest. The tool enabled that choice by making a much larger,
organized top of funnel practical.

## Operating results

**Professional operating evidence:** adoption, workflow change, candidate scale,
and the shift toward mailing-led prospecting come from the deployed workflow and
stakeholder feedback.

**Public implementation evidence:** repository source and automated checks
validate search, qualification, state, cost, campaign, audit, and revisit
controls independently of those operating observations.

- The company adopted the automated list-production and mailing workflow.
- Candidate production expanded from dozens of manual lookups to thousands of
  mapped records.
- Existing coverage, active prospects, and new candidates became visible in one
  operating view.
- Business-development effort shifted away from unsuccessful cold outreach and
  toward respondents who had already expressed interest.
- Onboarding volume increased after the mailing motion began, and the owner and
  BD users continued with the approach.

These are real operating observations from the professional workflow. The next
measurement layer is a dated campaign cohort that connects list source, mailed
volume, response, signed sites, productive sites, cycle time, and total
acquisition cost.

## Measurement plan

| Business question | Measure |
|---|---|
| Did the capacity ceiling move? | Unique candidates screened and mail-ready per analyst hour |
| Did quality hold at higher volume? | Share of candidates passing review, by market and source |
| Did the new motion improve BD productivity? | BD hours and cycle time per signed site, inbound vs. outbound |
| Is a campaign economically attractive? | Labor + API + mailing + onboarding cost per signed and productive site |
| Which markets and sources perform best? | Funnel conversion and total acquisition cost by campaign, source, and market |
| Is the workflow being used? | Searches, candidates reviewed, decisions completed, exports, and active users |

The companion [ATM prospecting cost model](https://yishao-ai.github.io/work-sample-evidence/Yi-Shao-ATM-Prospecting-Cost-Model.xlsx)
separates planning assumptions from closed-campaign outcomes and provides a
sensitivity view for labor and API usage.

## Next improvements

1. **Campaign attribution:** use campaign or market-specific QR / landing codes
   to connect mailed volume, responses, and signed contracts without creating an
   impractical code for every individual mail piece.
2. **Pre-run cost forecast:** continue refining the API usage and dollar estimate
   shown before each search begins.
3. **Stale-site review:** flag locations that have closed or materially changed
   before they enter a new campaign.
4. **CRM synchronization:** separate field ownership so map evidence, human
   decisions, and sales stage can synchronize without overwriting one another.
5. **Outcome dashboard:** close each campaign with conversion, cycle time, total
   acquisition cost, and productive-site results by market and source.

## Public implementation

The public repository exposes the map workflow, adaptive search, caching, spend
controls, qualification state, deduplication, pipeline history, revisit logic,
exports, and automated tests. It uses historical decommissioned locations and a
non-confidential demonstration pipeline; active operational records, employer
source code, credentials, and proprietary scoring logic are excluded.
