# Holdout v2.1 live evaluation outcome

Date: 2026-11-07
System: `rag-hybrid-v3`
Decision: **BLOCKED**

The single authorized execution stopped at the first technical error, as
required. It made three embedding and three generation requests, with no retry
or repeated question. Two positive cases completed; generation for `ihv2-p03`
timed out without a provider HTTP status. Thirteen cases were not executed.

Total runtime was 74,412.559 ms. `ihv2-p01` took 15,498.153 ms for retrieval,
11,485.676 ms for generation and 26,983.888 ms total. `ihv2-p02` took 2,424.947
ms, 4,029.928 ms and 6,454.934 ms respectively. For `ihv2-p03`, sanitized
runtime logs recorded a 6,731.7 ms embedding and 190.9 ms Qdrant read before
generation timed out; the exact failed generation duration is not persisted.

The two completed cases both retrieved their expected section at rank 1 as
semantic primary evidence. Their partial Recall@3 is 2/2, but this is not the
academic result over the required 12 positives and must not be reported as a
current-system pass. No negative case or the three-reference `ihv2-p12` case
ran, so negative abstention and 3/3 multisource coverage remain unmeasured.
The sanitized runtime log did show `ihv2-p03`'s expected high-demand section at
rank 1 with score `0.66478646`, but the result artifact does not retain
candidates once generation fails, so this is reported as log evidence rather
than a completed-case metric.

Custodial fidelity review passes `ihv2-p01` and fails `ihv2-p02`. The first
answer preserved both delivery windows and the customs limitation. The second
stated the correct 5% of that country's monthly fee but omitted the 90%
country-level monthly SLA and the two-consecutive-month trigger required by the
frozen rubric. No unsupported commercial claim was found in either answer.

Provider usage was 110 embedding tokens and 1,176 generation tokens (867
prompt, 309 completion). The provider returned generation cost
`0.00012468456`. The result artifact is immutable at SHA-256
`10367faa8fb5835f2a2dc09fa4a65b3caf5e3d878c8b3c48e9d3a6d49fa841b0`.

Because the authorization allowed exactly one execution and prohibited
retries, no continuation or replacement run was attempted. A new explicit
academic decision and a separately bounded authorization would be required for
any future action; this report does not recommend or authorize one.
