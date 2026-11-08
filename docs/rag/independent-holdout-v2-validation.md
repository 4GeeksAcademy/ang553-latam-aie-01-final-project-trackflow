# Holdout v2 custodial validation

Date: 2026-11-07
Target: `rag-hybrid-v3`
Decision: **BLOCKED pending versioned reference adjudication**

## Byte integrity

- Holdout SHA-256: `267112de491275f4beb98217377542e3910197b089262319671d9b4fa3be7ff2` — matches the reported value.
- Rubric SHA-256: `6c789afbde71a4d1a461ddec171b42f5bce54939d4bbd0443becbbbae4ad2ffb` — matches the reported value.
- Methodology SHA-256: `d36088397e4e843b2bb9fdba78f10689c8fe672825be687c3e56f5c420fc1474`.

Both JSON files parse successfully. They share the same evaluation and target
versions, contain 12 positive and four negative cases, use unique IDs, have an
exact one-to-one rubric ID set, give every case non-empty material facts, and
leave every negative expected-reference list empty.

## Corpus review

All positive questions are answerable from the four canonical documents. Their
required facts, figures, deadlines, currencies, exceptions and named approval
authorities are supported. The four negative questions ask for weekend opening
hours, refund timing, carrier insurance limits and box-dimension limits; none
of those facts is established by the corpus.

No question or material-fact defect was found. The multi-document California
Christmas case is answerable, provided the answer preserves the express-area
availability limit as well as the high-demand exception.

## Material reference incompatibility

The frozen expected references are not valid RAG payload identifiers. They use
Markdown paths such as
`docs/company-knowledge-base/trackflow-sla-delivery.en.md` and top-level
headings such as `Delivery SLA`. The live, deterministic chunk contract uses
IDs such as `sla-delivery` and semantic sections such as
`delivery-commitments`. None of the four declared reference pairs is among the
14 canonical Qdrant pairs.

The required versioned adjudication is:

| Case | Canonical expected pair or pairs |
|---|---|
| `ihv2-p01` | `sla-delivery/delivery-commitments` |
| `ihv2-p02` | `sla-delivery/monthly-sla-and-compensation` |
| `ihv2-p03` | `sla-delivery/high-demand-periods` |
| `ihv2-p04` | `returns-policy/standard-return-process` |
| `ihv2-p05` | `returns-policy/return-window-and-costs` |
| `ihv2-p06` | `returns-policy/international-returns` |
| `ihv2-p07` | `carrier-coverage/united-states-coverage` |
| `ihv2-p08` | `carrier-coverage/spain-coverage` |
| `ihv2-p09` | `carrier-coverage/selection-and-exceptions` |
| `ihv2-p10` | `storage-pricing/standard-rates-and-grace-period` |
| `ihv2-p11` | `storage-pricing/long-term-inventory` |
| `ihv2-p12` | `carrier-coverage/united-states-coverage`, `sla-delivery/delivery-commitments`, and `sla-delivery/high-demand-periods` |

The last case currently declares only two broad document references but needs
three chunk-level references to measure complete evidence coverage. Applying
this mapping inside the runner would silently change the frozen scoring target.
The v2 files therefore remain unchanged. The evaluator or authorized academic
custodian must issue new versioned filenames and hashes (for example v2.1),
preserving the questions and facts unless it independently decides otherwise.

## Academic requirement review

The supplied academy briefing requires at least eight questions, coverage of
all four documents, a high-demand case and measured Recall@3 of at least 80%.
It does not expressly require a strictly isolated author. The methodology's
disclosed exposure to general repository context is therefore a limitation to
report, not an additional automatic rejection criterion. It must not be
relabeled as strict package-only independence.

## Frozen-system verification

All 29 entries in `rag-hybrid-v3-freeze-manifest.json` match their SHA-256 and
byte sizes. Effective configuration remains threshold `0.57`, vector size
1024, the approved embedding and generation model IDs, a two-second request
interval and zero HTTP retries. Qdrant is healthy with 14 points, Cosine
distance and 1024-dimensional vectors. The private 4Geeks key is present and
the user reports it was rotated; actual gateway acceptance remains untested.

## Execution status

The runner was not rebound to v2 because doing so would encode an unapproved
post-freeze reference translation. No result file was created and no external
request was made. Once a corrected, rehashed version is received, the existing
guarded execution contract can enforce at most 16 embeddings and 16
generations, sequential requests, a minimum two-second interval, zero retries,
atomic per-case persistence, refusal to overwrite and stop-on-technical-error.
