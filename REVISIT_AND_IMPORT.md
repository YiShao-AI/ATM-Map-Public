# Revisit rules and controlled-import requirements

The repository implements structured rejection reasons, decision history, and a
reason-specific revisit queue. This document also defines the controls that must
be satisfied before high-volume legacy records are accepted through an inbound
import interface.

## Business requirement

A rejection is not one uniform outcome. Some reasons describe temporary timing;
others describe a durable property of the site; and a do-not-contact instruction
must remain outside ordinary reactivation. The workflow needs to preserve those
differences so a previously reviewed territory can be worked again without
repeating every decision from the beginning.

## Implemented rejection vocabulary

| Reason code | Operating meaning | Default revisit behavior |
|---|---|---|
| `no_response` | Outreach produced no response | revisit after 6 months |
| `unreachable` | A decision-maker could not be reached | revisit after 3 months |
| `owner_declined` | Owner declined the current offer | revisit after 12 months |
| `rate_terms` | Commercial terms did not work | revisit after 12 months or after an offer change |
| `chain_hq` | Local staff cannot authorize the decision | revisit after 12 months or with a central agreement |
| `competitor_atm` | Another machine is already installed | revisit after 24 months |
| `safety` | Area or site risk requires a later review | revisit after 24 months |
| `no_indoor_space` | Physical layout cannot support the machine | permanent for the reviewed site condition |
| `closed` | The reviewed business is closed | permanent for that business; a new tenant is a new candidate |
| `other` | Structured category is insufficient | revisit after 12 months with the note retained |
| `do_not_contact` | Standing instruction, represented as a status | no automated revisit |

Intervals are defaults held in one vocabulary in [`store.py`](store.py), so a
business-approved policy change does not require rewriting historical records.

## Revisit workflow

```text
reject candidate
  → require reason code
  → calculate temporary or permanent disposition
  → append decision and event history
  → hide from ordinary new-candidate results
  → surface on the due date in Revisit
  → user requalifies, rejects again, or preserves the stop
```

The due queue includes only current rejected records that are non-permanent,
past their revisit date, still associated with a present business, and not
blocked by a structural location rule. Requalifying a record clears obsolete
revisit and permanent flags while retaining the earlier decision in history.

## Business rules

| ID | Rule |
|---|---|
| RV-01 | Rejection requires a structured reason; optional free text adds context but does not replace the code. |
| RV-02 | The reason vocabulary determines the default revisit date and permanence. |
| RV-03 | Only the latest unsuperseded decision controls current state. |
| RV-04 | Earlier decisions remain queryable for attempt and outcome analysis. |
| RV-05 | Do not contact is never cleared by search, campaign transition, or bulk import. |
| RV-06 | Leaving rejected state clears stale revisit and permanence flags. |
| RV-07 | A new tenant can be reconsidered without erasing the prior tenant’s history. |
| RV-08 | Manual Ready-to-mail approval is removed when a site is rejected. |

## Controlled-import target behavior

High-volume import is a state-management operation, not only CSV parsing. The
required sequence is:

```text
upload → validate → stage → match → dry-run report → review exceptions
       → approve → commit in one transaction → retain batch audit
```

### Matching order

Use the least expensive and most deterministic evidence first:

1. exact `place_id` when supplied;
2. existing CRM ID when the systems are already linked;
3. exact normalized address;
4. business name and address within the same ZIP;
5. paid geocoding only for unresolved rows, within an approved call budget.

Ambiguous matches route to human review. They are never guessed or silently
merged.

### Import conflict policy

- The import carries source, source timestamp, and batch ID.
- A record older than the current human decision may be retained as history but
  cannot replace current status.
- A bulk stage follows the same monotonic progression as every other write.
- Blank inbound values cannot erase known provider or manually entered fields.
- Do-not-contact cannot be cleared by any import action.
- Re-running the same batch is idempotent.
- Commit is transactional, and the batch summary remains available for audit.

### Required dry-run report

Before approval, the user receives counts and row-level exceptions for:

- total rows;
- exact matches;
- new records;
- ambiguous matches;
- malformed or incomplete rows;
- current decisions protected from overwrite;
- do-not-contact records protected;
- estimated paid lookups; and
- resulting campaign or stage changes.

## Acceptance scenarios

| ID | Given | When | Then |
|---|---|---|---|
| UAT-R01 | a temporary rejection whose revisit date has passed | the user opens Revisit | the site appears with its reason and prior decision date |
| UAT-R02 | a permanent rejection | the user opens Revisit | the site does not appear |
| UAT-R03 | a rejected site is requalified | the user returns it to prospect or pipeline | stale revisit and permanent flags clear, and the earlier decision remains in history |
| UAT-R04 | a Verify-first site is manually approved | the user selects Ready to mail | the record enters Ready to mail and displays manual-verification evidence |
| UAT-R05 | a manually approved site is rejected | the rejection is saved | manual approval clears automatically |
| UAT-I01 | an import contains an older stage than the current site | dry run and commit occur | current stage remains; the attempted update is reported and logged |
| UAT-I02 | an import contains an unknown identifier | dry run occurs | the row is classified as new or unresolved; no nameless site is created |
| UAT-I03 | an import contains a possible duplicate | matching is inconclusive | the row enters review and is not merged automatically |
| UAT-I04 | the same batch is submitted twice | the second commit runs | no duplicate site, decision, or campaign transition is created |
| UAT-I05 | an import contains formula-like spreadsheet values | an export is generated | the values are escaped so opening the CSV cannot execute a formula |

The implemented revisit behavior is covered by `RevisitMaths`,
`DecisionHistory`, and `ManualVerification` in
[`tests/test_store.py`](tests/test_store.py). The controlled-import scenarios are
the acceptance gate for a future inbound-import endpoint; current CSV behavior
is outbound export only.
