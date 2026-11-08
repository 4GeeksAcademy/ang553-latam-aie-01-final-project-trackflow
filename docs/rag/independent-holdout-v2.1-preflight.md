# `rag-hybrid-v3` holdout v2.1 preflight

Date: 2026-11-07
Decision: **READY FOR LIVE EVALUATION AUTHORIZATION**

The TrackFlow responsible user subsequently approved the holdout and rubric in
an explicit attributable message. The accepted review is classified as
`prospective_with_independence_limitations`, not strictly independent. Its
`execution_authorized` field remains `false`; approval of the review does not
authorize gateway consumption.

## Frozen inputs

- Holdout: `independent-holdout-v2.1.json`
- SHA-256: `99a37a949d1b5adc5c01704778eef43c9611981a3541fb7e42af50c0171edc71`
- Rubric: `independent-holdout-v2.1-rubric.json`
- SHA-256: `d05d1fe6e5a970816c3a201307fa05876fc03f1661c5662e3392eeb90d0cca5d`
- Binding SHA-256: `c269a13124f72561c07c3fcd555ed84f9f62bf2adc8fa828ebf2c2266667c207`

The 12 positive and four negative IDs match the rubric exactly. Questions, IDs
and material facts match v2 byte-for-byte at the corresponding JSON values.
All 14 declared expected pairs belong to the canonical Qdrant contract.
`ihv2-p12` has three expected pairs: United States carrier coverage, delivery
commitments and high-demand periods.

The v2 and v2-rubric files retain their original hashes and remain unchanged.

## Instrumental runner binding

The guarded runner and validator were generalized to load a local binding that
fixes artifact filenames, hashes, case counts, result filename and request
budget. Per-case output now distinguishes academic Recall@3 (any expected pair
in the raw top three) from reference coverage. It records expected count, raw
and selected hit counts, coverage ratios and complete-coverage booleans. Thus
`ihv2-p12` can report `3/3` separately from its single question-level Recall@3
contribution.

This adjustment does not change retrieval, lexical rules, chunking, prompts,
corpus, model IDs, threshold or Qdrant. The four instrumental files are listed
explicitly in the second freeze manifest; all 29 manifest entries match.

The pending review template is not an approval. The runner rejects it before
loading live settings and creates no result. Before execution, an attributable
reviewer must truthfully complete the hash-bound fields and preserve the
reported limitation: the separate evaluator saw general repository context,
although it reported no access to RAG code, prompts, prior questions, results,
logs or system outputs. The academy briefing does not expressly require strict
package-only isolation. A non-independent review is therefore accepted only
when it is explicitly labeled `prospective_with_independence_limitations` and
acknowledges that limitation; a strict-independence label requires a stated
basis.

## Offline evidence

- RAG core and evaluation runner: 51 passed.
- RAG API, authentication and sanitized failures: 11 passed.
- Offline binding validation: passed; no result file created.
- Unapproved-review gate: blocked as designed; no gateway call.
- Qdrant: healthy, 14 points, 1024 dimensions, Cosine.
- Effective threshold: `0.57`.
- Effective models: approved Perplexity embedding and DeepSeek generation IDs.
- Private 4Geeks key: present; user reports rotation; functionality not tested.

## Live boundary

The review gate is approved. Real execution remains subject to a new explicit
consumption authorization. The command is prepared but must not be run during
preflight:

```powershell
uv run python -m pipelines.eval.rag.run_independent_holdout `
  --execute `
  --binding pipelines/eval/rag/independent-holdout-v2.1-binding.json `
  --review-record <completed-hash-bound-review.json> `
  --approved-budget 32
```

The maximum is 16 embeddings plus 16 generations. Requests are sequential,
the provider interval is at least two seconds, HTTP retries are zero, output is
persisted after each case, an existing result is never overwritten, and the
run stops at the first technical error. Commercial fidelity remains pending
attributable human fact-by-fact adjudication after the run; it is not converted
into an automatic favorable score.
