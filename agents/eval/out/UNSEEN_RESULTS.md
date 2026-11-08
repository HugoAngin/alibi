# Frozen unseen evaluation: interrupted capture with authorised continuation

The capture is finished. All 132 case/system groups have a retained outcome. This is a negative result: none of the three systems achieved a safe-complete main or adversarial case. It does not support a claim that Alibi outperforms either baseline.

Application candidate: `58bf0d4a199f5ad3833fa9bde751d31140219975`. Capture evaluator: `8af976b19eb2fb310f129e6cfee9f13ff36703e6`. Three variants were tried before this freeze. The result recorder stamped its end-of-command checkout `498014a`; this contains later reporting changes, while `run.json` retains the actual launch evaluator above.

| System | Attempted cases | Returned without worker failure | Failed cases | Main safe-complete, official | Main safe-complete, second scorer | Adversarial safe-complete |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agents | 44 | 10 | 34 | 0/32 | 0/32 | 0/12 |
| one_pass | 44 | 10 | 34 | 0/32 | 0/32 | 0/12 |
| rules | 44 | 10 | 34 | 0/32 | 0/32 | 0/12 |

The 32 main cases produced 76 failed request outcomes per system. The separate 12-case adversarial slice produced two failed request outcomes per system. A process returning without failure is not a correctness pass. Earlier generic child-exit causes remain unconfirmed; the public unpaused reproduction did not reproduce them. They count against the candidate, with no selective retries.

Original attempt: 70 jobs attempted, 67 returned, three scheduler PermissionErrors and 62 unstarted. I manually suspended the coordinator while investigating earlier exits; this exposed a macOS process-group cleanup race and affected four in-flight timings. The public reproduction used the same frozen application, scheduler, sandbox and limits with stderr retained. The evaluator fix reaps exited children, bounds cleanup waits and retains private stderr.

Under lead rules 7 and 8, the continuation ran exactly 62 unstarted jobs and retried only the three publicly reproduced scheduler errors. It preserved every one of the 67 original returned groups, including failures, byte-for-byte. All 65 continuation jobs returned; there were zero new scheduler exceptions, no deadline stop and no reduction from four workers. This is 135 execution attempts across 132 distinct groups. The original capture and lock remain intact.

Limits remained agents 180 seconds, one-pass 90 seconds and rules 30 seconds. Lead extended the hard stop to 12:50 UTC; the capture finished at 11:49:59, before the 12:05 checkpoint. Candidate, sealed commitments, model configuration and original lock were verified again before and after the continuation.

| System | Known completed model calls | Known tokens | Partial estimated USD | Workers with unknown full usage |
| --- | ---: | ---: | ---: | ---: |
| agents | 62 | 301878 | 0.70011125 | 34 |
| one_pass | 14 | 60731 | 0.05917425 | 34 |
| rules | 0 | 0 | 0.00000000 | 34 |

Full-run usage and cost are unknown. These partial totals use retained completed calls and repository pricing assumptions; they are not a provider invoice. Unknown or unlogged original attempts are not free. Timing is qualified by the operator pause.

Both scorers returned zero safe-complete main cases. They share the same capture, so agreement is not independent validation. The second scorer requires the exact per-question citation set; the official scorer accepts an alternative sufficient set. The public rehearsal exposed that distinction. Headline scoring concerns top-level investigation/draft-evidence claims, not final recipient correctness.

Package-claim comparison is unavailable for all 92 RFCs per system. The recorded zero divergences must not be presented as agreement. Retrieval differs between the frozen baselines, and this synthetic held-out pack received rules calibration during authoring. No prompt, gold, input or candidate was changed after opening.

Public security evidence is separate: 19/47 probes were exercised, 28 were not run, SP-23 and SP-24 failed, and both acceptance controls passed. The configured passport-store result does not cover the separately observed plaintext bank delivery state. Frozen local query-token logging is a stated limit; the hosted fix is separate.

Replay verification: interrupted record `1b235c16e9` PASS; continuation `e16afeeac5` PASS. Aggregate export record `f8dc5763b6` is separately verified. Replay verifies retained hashes and saved metrics without new model calls. For commands and provenance see `results.jsonl`, `verified.jsonl`, `UNSEEN_AGGREGATES.json`, `PERMISSION_FIX.md` and `PERMISSION_FIX_PROOF.json`. The raw sealed evidence remains private.
