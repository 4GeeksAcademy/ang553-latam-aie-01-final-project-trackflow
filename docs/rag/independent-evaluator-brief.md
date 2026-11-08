# Independent evaluator brief for `rag-hybrid-v3`

## Purpose

Create and freeze an academically independent evaluation set for TrackFlow's
final semantic plus lexical RAG implementation. The evaluator must not inspect
the implementation, prompts, vector scores, historical evaluation results,
previous questions, human-test questions, or defect reports before the new set
is sealed.

This brief and the four source documents listed below are the complete allowed
input package. No answer should rely on knowledge outside those documents.

## Allowed source package

- `docs/company-knowledge-base/trackflow-sla-delivery.en.md`
- `docs/company-knowledge-base/trackflow-returns-policy.en.md`
- `docs/company-knowledge-base/trackflow-carrier-coverage.en.md`
- `docs/company-knowledge-base/trackflow-storage-pricing.en.md`
- this brief

The package custodian must verify the four document hashes against
`pipelines/eval/rag/rag-hybrid-v3-freeze-manifest.json` before delivery. Do not
send any other repository file to the evaluator.

## Independence requirements

The author must not have implemented, tuned, debugged, or previously evaluated
the RAG system. The author must work only from the allowed package and must not
ask the implementation team for likely failure cases. Questions, expected
references, and scoring facts must be fixed before the implementation team sees
them or any model call is made.

If possible, use a second person who is also independent from implementation to
review references, ambiguity, duplicates, material facts, and prior exposure.
If one evaluator performs both roles, disclose that limitation explicitly; do
not describe it as a two-person review.

After sealing, a custodian may compare the new set with historical material to
disclose exact or intent overlap. Overlap must never be resolved by tuning the
system or silently rewriting the set. A defective set receives a new version,
new filenames, and new hashes.

## Required set

Prepare exactly 16 cases to fit the authorized ceiling:

- 12 positive questions grounded in the source package;
- four negative questions whose requested answer is absent from all four
  documents.

The positive cases must collectively cover all four documents, with at least
two cases per document, and must include high-demand periods. Include both
single-section questions and questions that genuinely require two related
sections. Do not manufacture ambiguity merely to force complementary context.

Questions should be natural user questions, not copied sentences. Avoid clues
such as source filenames, section identifiers, expected carrier names, or
answer templates unless they would naturally appear in the question.

## Required sealed artifacts

Create two UTF-8 JSON files before execution:

1. A holdout with an immutable evaluation version and `positive` and `negative`
   arrays. Every case requires a unique `id`, a `question`, and `expected` pairs
   of `source_document` and `section`. Negative `expected` arrays are empty.
2. A rubric with the same evaluation version and one entry per case. Each entry
   contains the case `id` and all `required_facts` needed for a commercially
   faithful answer or abstention.

Calculate SHA-256 over the exact bytes of both files. Record filenames, hashes,
case counts, author, creation time, allowed inputs, and limitations in a signed
or attributable review record. Do not put credentials in any artifact.

## Metrics and adjudication

Report these separately; do not merge them into one favorable score:

- raw semantic Recall@3 on the 12 positive cases: at least one expected pair in
  the raw Qdrant top three;
- expected evidence selected as semantic primary;
- expected evidence selected through the bounded lexical rule;
- expected complementary evidence selected;
- combined selected evidence coverage;
- top-three relevance/precision, adjudicated against the frozen references;
- negative retrieval abstention and final answer abstention;
- false positive and false negative counts;
- commercial fidelity against every frozen material fact;
- unsupported or tangential commercial claims;
- technical error rate, per-stage latency, provider HTTP status category, and
  provider usage when returned.

The academic retrieval target remains Recall@3 of at least 80%. Passing that
target does not override a fidelity defect, an incorrect negative answer, or a
technical failure. A technical error is not an abstention. Fidelity is not
scored automatically: an independent reviewer must assess material facts,
figures, deadlines, fees, scope, exceptions, and approval requirements.

## Execution boundary

Do not execute the set. Return the sealed artifacts and review record to the
custodian. The implementation remains frozen from that moment. The custodian
must adapt the guarded runner only to load the new filenames and hashes, without
changing retrieval, prompts, chunking, corpus, threshold, models, or grading.
Any implementation change after exposure invalidates the evaluation attempt
and requires a newly authored holdout.
