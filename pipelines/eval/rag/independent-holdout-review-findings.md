# Holdout review findings before execution

Status: **not independently approved**

The sealed files retain their original hashes and were not edited during this
review. Structural validation passes: 12 positive cases, four negative cases,
unique IDs and exact question strings, all four canonical documents, and a
high-demand case. Every declared positive reference and every material rubric
fact is supported by the canonical corpus. No material ambiguity was found in
the expected answers.

## Independence limitation

The implementation-side agent authored and inspected this set. It therefore
cannot act as its independent reviewer. Exact-string comparison finds no reuse
of the frozen historical questions, but intent-level comparison finds at least
these overlaps:

| Sealed case | Prior inspected intent |
|---|---|
| `v2-sla-express-corridor` | express availability and timing calibration/generation |
| `v2-sla-seasonal-notice` | high-demand/Black Friday generation and retrieval |
| `v2-returns-shipping-liability` | return costs and TrackFlow-error liability |
| `v2-carrier-spain-small-parcel` | Spain under-2-kg carrier calibration |
| `v2-carrier-selection-governance` | carrier exception and Carlos Vega approval |
| `v2-storage-country-rates` | standard storage rates and grace-period generation/calibration |

The lexical rule was justified from separate human failures involving SLA
compensation and rural Aragón carrier selection, not from executing this set.
However, because the implementer had already seen the sealed questions before
`rag-hybrid-v3` was finalized, absence of indirect influence cannot be proven.

## Required human adjudication

The recommended academically conservative treatment is:

1. retain this artifact unchanged as a prospective regression/secondary set;
2. have an isolated evaluator decide whether its disclosed overlap is
   acceptable for the course requirement; and
3. if strict independence is required, commission a new versioned holdout from
   an evaluator who has not seen implementation decisions or human test cases.

Do not mark the supplied review template approved until that adjudication is
complete. If a defect requires replacement, create new filenames and hashes;
never rewrite these sealed artifacts or their historical record.
