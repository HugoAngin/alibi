# Candidate 5: post-fix second look on reused cases

The full capture finished with132 distinct case/system jobs attempted once, before the13:25 UTC stop. This is development evidence on a reused, previously scored synthetic pack; it is not a fresh unseen benchmark. The original58bf0d4 frozen negative result remains unchanged. Rules scored highest in this second look; the result does not establish an advantage for the agent system.

Candidate: `3c5047639aade89f1916163ae2ad3a00050d1143`. Launch evaluator: `af751cdb001be8a4debe76683736f33ee2bdd00d`. Same original model configuration and case/gold commitments; an explicitly declared synthetic issuer map and seed were applied identically in all three modes. No hidden-case mapping or gold edits. Limits: agents180s, one-pass90s, rules30s; four isolated workers.

| System | Runtime complete | Worker failures | Main official | Main second scorer | Adversarial official | Adversarial second scorer |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agents | 38/44 | 6 | 6/32 | 3/32 | 0/12 | 0/12 |
| one_pass | 44/44 | 0 | 5/32 | 2/32 | 0/12 | 0/12 |
| rules | 44/44 | 0 | 9/32 | 4/32 | 2/12 | 2/12 |

All six agent worker failures were timeouts and remain in the denominators. No worker was retried. There were zero scheduler errors, no deadline truncation and no worker reduction. Runtime completion is separate from answer correctness.

| System | Completed logged model calls | Logged tokens | Estimated USD from retained usage | Completeness |
| --- | ---: | ---: | ---: | --- |
| agents | 322 | 1941135 | 4.267921 | Partial; six failed workers may have unlogged usage |
| one_pass | 92 | 628756 | 0.661911 | Complete retained worker usage |
| rules | 0 | 0 | 0.000000 | Complete retained worker usage |

Cost estimates use repository pricing assumptions, not a provider invoice. Main-slice request-level cost remains unavailable for agents and one-pass where request fields are incomplete; the table instead reports the separately retained full44-case worker usage.

Replay `2187aa7c34` PASS reconstructs the exact launch evaluator, verifies retained capture commitments and forbids new worker execution. Initial recorder `70337513e6` remains recorded with a replay ERROR because it stamped the checkout at command finish rather than the launch evaluator; no prediction or original record was replaced. Aggregate export `78ffecd2c2` PASS. Original capture integrity `752f72f28a` PASS after this run.

Headline accuracy scores investigation/draft-evidence claims, not final recipient correctness after policy filtering. The two scorers share retained evidence and differ in their sufficient-evidence rules; their agreement or disagreement is not independent validation. The original scoring versions remain fixed. Known future scorer-hardening patches remain separate from these numbers.

Public probe evidence is separate: both bank delivery controls passed,15 tested refusals held, SP-23 and SP-24 failed, and28 catalogue probes were unexercised. The passport-store encryption claim does not cover plaintext recipient bank state. Later public fixes and supplemental probes are outside this candidate.

Later public development reaches75/80 rules cases with record `98b04514f5` PASS, but those changes were not in candidate5. Further reused-input diagnostics must use a new immutable candidate, separate output and an explicit development label.
