# Independent holdout v2: methodology and review record

## Identification

- Evaluation version: `trackflow-independent-holdout-v2-2026-11-07`
- Target system version: `rag-hybrid-v3`
- Author: OpenAI Codex, acting as the independent evaluation-set author
- Created: `2026-11-07T14:25:05-07:00` (America/Hermosillo)
- Status: authored and statically validated; not executed against any model, retriever, RAG system, or gateway

## Allowed evidence

The questions, references, and scoring facts were derived only from:

- `docs/company-knowledge-base/trackflow-sla-delivery.en.md`
- `docs/company-knowledge-base/trackflow-returns-policy.en.md`
- `docs/company-knowledge-base/trackflow-carrier-coverage.en.md`
- `docs/company-knowledge-base/trackflow-storage-pricing.en.md`
- `docs/rag/independent-evaluator-brief.md`

No source code, prompt, vector score, prior holdout, prior question file, model
output, RAG result, or development conversation was opened or used to design
the cases. The package manifest was used only to identify and verify the
allowed inputs.

## Method

The set contains exactly 16 natural-language cases: 12 positive questions and
four negative questions. Every positive case has an exact document-and-section
reference. Each of the four source documents is represented by at least two
positive cases. The set includes direct single-document questions and one
multi-document question that genuinely combines carrier choice with delivery
SLA and the Christmas high-demand exception.

Negative cases ask for warehouse weekend hours, refund timing, carrier
insurance limits, and box-dimension limits. None of those facts appears in the
four allowed documents. Their rubric requires an explicit knowledge
limitation and prohibits plausible but unsupported invention.

The rubric keeps retrieval and answer fidelity separate. Retrieval covers raw
semantic Recall@3, evidence-selection paths, complete reference coverage,
top-three precision, and negative retrieval abstention. Fidelity is assessed
fact by fact and also requires the absence of contradictions and unsupported
commercial claims. Technical errors are recorded separately and never count
as abstentions.

## Static validation

The artifacts were checked for valid UTF-8 JSON, matching immutable evaluation
versions, unique case identifiers, a 12/4 positive-negative split, one rubric
entry per case, empty references for negatives, valid source paths and exact
top-level section names. Source package hashes were checked against the
independent-evaluator package manifest. SHA-256 hashes below bind the exact
bytes of the final JSON files.

## Limitations and authorship disclosure

This was a single-author exercise; no second independent reviewer adjudicated
ambiguity, duplicates, references, or prior exposure. Repository operating
policy required reading the project startup context, including a high-level
progress record that mentions the existence and broad status of an earlier
evaluation. No earlier evaluation artifact, question, rubric, or result file
was opened. Because that startup exposure falls outside the minimal evaluator
package, strict package-only isolation cannot be claimed without qualification.
The custodian should treat this disclosure as an academic limitation and may
commission a second, fully isolated review before execution.

The source documents have only one explicit top-level section each, so all
references use those exact headings. The rubric preserves the wording “150%
over the standard rate” for long-term inventory and does not introduce an
unstated arithmetic interpretation. The set has not been compared with prior
questions for intent overlap; such a comparison belongs to the custodian only
after sealing and must not lead to tuning or silent rewriting.

## Frozen artifacts and hashes

- `pipelines/eval/rag/independent-holdout-v2.json`: `267112de491275f4beb98217377542e3910197b089262319671d9b4fa3be7ff2`
- `pipelines/eval/rag/independent-holdout-v2-rubric.json`: `6c789afbde71a4d1a461ddec171b42f5bce54939d4bbd0443becbbbae4ad2ffb`

Case counts: 12 positive, four negative, 16 total.
