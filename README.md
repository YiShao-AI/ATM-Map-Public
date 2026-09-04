# ATM host-site prospecting map

A map-based operating system for finding, qualifying, and managing prospective
retail ATM host sites. It replaced one-at-a-time map lookups with a repeatable
workflow capable of surfacing and organizing thousands of candidates while
keeping coverage, uncertainty, pipeline state, and API spend visible.

**Live application:** <https://atm-map.taila60f3a.ts.net/>

The application is password protected and uses metered APIs. Login credentials
are supplied with the work-sample application; searches should be kept focused.

## At a glance

| | |
|---|---|
| **Operating problem** | Business development built prospect lists one location at a time and could not reliably see searched territory, missed candidates, duplicates, or prior review state. |
| **Primary users** | Company owner and business-development team. |
| **Workflow change** | Define a market, preview usage, run adaptive search, review evidence, qualify, deduplicate, decide, revisit, and export. |
| **Observed result** | Candidate production expanded from dozens of manual lookups to thousands of mapped records, enabling a broad mailing-led prospecting strategy. |
| **My role** | Discovery, requirements, workflow and data design, implementation, cost controls, validation, and rollout. |

## Operating workflow

```text
scope market → preview API usage → run adaptive search → review evidence
             → qualify and deduplicate → decide → advance / revisit / export
```

Existing locations, pipeline prospects, and newly discovered candidates share
one operating view. That makes coverage gaps and possible duplicate outreach
visible before the next search or campaign decision.

## Core capabilities

- ZIP, pin-and-radius, and drawn-area search;
- adaptive subdivision of dense result areas;
- one map for decommissioned fleet history, active pipeline, and new candidates;
- evidence-based qualification that preserves missing data as unknown;
- conservative co-location handling that keeps adjacent businesses distinct;
- manual verification that can move a `Verify first` candidate to `Ready to mail`;
- durable search records with short IDs, category checkpoints, exact-result
  reopening, direct links, and linked reruns;
- provider-failure handling that keeps completed-category results visible and
  reusable instead of discarding paid work;
- structured decisions, ownership, stages, history, and reason-specific revisit dates;
- monotonic funnel controls that prevent stale bulk updates from moving a deal backward;
- governed CSV export with spreadsheet-formula injection protection; and
- pre-run forecasts, cache reuse, daily and lifetime API limits, and a kill switch.

The current field mask requests hours, phone, rating, review count, and website,
so Google bills it as **Places Text Search Enterprise**, not Pro. The embedded
planning rate is `$35 / 1,000` with a 1,000-event monthly free-usage cap, checked
against Google's global price list on 2026-09-03. The in-app free-tier figure is
an application-local estimate; Google Cloud Billing remains authoritative when
other projects share the billing account.

## Business-analysis evidence

- [`BUSINESS_CASE.md`](BUSINESS_CASE.md) — stakeholder problem, before/after
  workflow, requirements, operating results, and KPI plan.
- [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md) — current data model, ownership,
  lineage, integration boundaries, and integrity controls.
- [`DATA_HUB.md`](DATA_HUB.md) — campaign workflow, system handoffs, state
  transitions, exceptions, and operational acceptance criteria.
- [`REVISIT_AND_IMPORT.md`](REVISIT_AND_IMPORT.md) — revisit rules and the
  controlled-import requirements for high-volume legacy data.
- [`DELIVERY_AND_READINESS.md`](DELIVERY_AND_READINESS.md) — discovery
  synthesis, user stories, delivery decisions, risks, owners, and launch gates.
- [`VALIDATION_RESULTS.md`](VALIDATION_RESULTS.md) — recorded core and
  active-service test results mapped to operating risks.
- [`benchmarking/README.md`](benchmarking/README.md) — a bounded, reproducible
  225-call market-search and cache-evidence protocol (safe/dry by default).
- [`DEPLOY.md`](DEPLOY.md) — protected deployment, reset, monitoring, recovery,
  and smoke-test runbook.
- [ATM prospecting cost model](https://yishao-ai.github.io/work-sample-evidence/Yi-Shao-ATM-Prospecting-Cost-Model.xlsx)
  — planning assumptions, capacity, sensitivity, and closed-campaign outcome inputs.

## Implementation map

| Capability | Source | Verification |
|---|---|---|
| Adaptive search, caching, and API budgets | [`proxy.py`](proxy.py) | [`tests/test_search.py`](tests/test_search.py) |
| Qualification and interactive workflow | [`index.html`](index.html) | search and store suites |
| SQLite data model, decisions, campaigns, and audit events | [`store.py`](store.py) | [`tests/test_store.py`](tests/test_store.py) |
| CRM field and stage mapping | [`crm_adapter.py`](crm_adapter.py) | isolated behind a configurable integration seam |
| Historical decommissioned ATM layer | [`atms.js`](atms.js) | serial-number-to-place links cached in SQLite |

## Run locally

Python 3.9+ is the only runtime dependency.

```bash
cp .env.example .env
# Add restricted keys to .env for live Google search and map tiles.
python3 proxy.py
```

Open <http://127.0.0.1:8093>. Without `MAPS_BROWSER_KEY`, the application uses
its non-Google basemap. Live candidate search requires
`GOOGLE_MAPS_API_KEY`; the committed `.env.example` contains no credentials.

For a protected public tunnel:

```bash
DEMO_USER=demo DEMO_PASS='<strong unique password>' ./start-demo.sh
```

## Verification

```bash
python3 -m unittest discover -s tests -v
```

Current result: **80 tests pass with the service stopped; 83 pass with the local
service active**, including three HTTP access-boundary checks. A separate
Chromium flow verifies Search History, openable rows, reopening, direct links,
formula-safe CSV export, terminal states, checkpointed results after a provider
failure, and a zero-call cached rerun. The full record is in
[`VALIDATION_RESULTS.md`](VALIDATION_RESULTS.md).

## Data boundary

The demonstration retains 33 historical ATM records decommissioned by early
2026, including their serial numbers and addresses, because they provide the
credible existing-location layer used by the map. The pipeline records are
non-confidential demonstration data. Active operational records, credentials,
access logs, employer source code, and proprietary ranking criteria are not in
this repository.
