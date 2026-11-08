# Independent holdout review protocol

The sealed input and rubric must not be edited during review. Their expected
SHA-256 values are recorded in the review template and evaluation plan.

An evaluator who did not implement or tune `rag-hybrid-v3` must:

1. Run `uv run python pipelines/eval/rag/run_independent_holdout.py` without
   `--execute`. This validates structure and prints the sealed hashes offline.
2. Compare every positive reference and material fact with the four canonical
   files in `docs/company-knowledge-base/`.
3. Check ambiguity, duplicate intent, overlap with historical questions, and
   whether any case was previously used to tune threshold, prompts, lexical
   rules, or retrieval behavior.
4. Record every limitation. Do not repair the sealed artifacts in place; a
   defect requires human adjudication and a newly versioned set.
5. Copy the review template outside the implementation workflow, complete it,
   and set approval fields to `true` only when supported. The runner rejects an
   incomplete, non-independent, mismatched, or unapproved record.

After private credential rotation is verified and the user explicitly approves
the external budget, execute exactly once:

```text
uv run python pipelines/eval/rag/run_independent_holdout.py --execute \
  --review-record <approved-review.json> --approved-budget 32
```

The run is sequential, uses the configured minimum two-second provider
interval, has zero HTTP retries, refuses to overwrite an existing result, and
stops on the first technical error. It records raw semantic top-three recall,
primary/complementary selection, negative abstention, false positives, latency,
request counts, provider usage, answers, and a pending fidelity adjudication.
Commercial fidelity must be scored independently against the sealed rubric;
the implementation must not be changed from holdout outcomes.
