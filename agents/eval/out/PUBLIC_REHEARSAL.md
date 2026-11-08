# Public live rehearsal

Candidate: `c6ea270164ac992e7b5f3c0869a79d3d56bf74c8`. Public cases 1 and 2 were captured through disposable per-case/system workers. This is a public rehearsal, not an unseen-data result. The report command scores retained outputs and does not call a model or open hidden cases.

Reproduce from the repository root:

```sh
uv run --frozen python -m agents.eval.public_rehearsal_report
```

The final JSON line retains the sequential capture's original headline metrics and adds schema counts, policy field/status checks, retained usage estimates and the existing second scorer's safe-complete counts. Metrics prefixed `parallel_` describe the separately retained second capture below. Both captures run through `score_saved` in temporary directories; committed captures are not modified. Replay depends on the public cases and scoring code in the checked-out commit.

## Sequential capture

| Result | agents | one_pass | rules |
| --- | ---: | ---: | ---: |
| Official safe-complete cases | 0/2 | 0/2 | 1/2 |
| Existing second scorer safe-complete cases | 0/2 | 0/2 | 0/2 |
| Forbidden items | 0 | 0 | 0 |
| Unnecessary items | 2 | 19 | 1 |
| Recorded model calls | 13 | 4 | 0 |
| Recorded tokens | 111,836 | 43,586 | 0 |
| Retained usage estimate, USD | 0.2330655 | 0.0481935 | 0 |

The second scorer disagrees on the rules baseline; its results are not evidence of independent corroboration. All 12 main predictions pass the prediction schema, and all six worker records report no failure and a passed isolation preflight. These capture records do not themselves establish a general isolation guarantee.

Agents have correct status/citation results in these cases but omit required structured facts. The pending-settlement case also retains attachments where the public gold expects an empty package. These are candidate performance failures. One-pass has no follow-up retrieval; unequal retrieval access must remain visible in any comparison. Neither public case represents broad coverage or supplies a confidence interval.

Lead interpretation, confirmed in board comment `6720425065`: headline correctness describes investigation/draft evidence from top-level `claims`. It does not establish recipient correctness after policy review. In the rules policy variant, `amount_pence` fields inside final packages fall from 30 to 0; all four policy prediction rows validate. Across two RFCs, three final `package_claims` statuses change to unresolved while the corresponding top-level claims remain supported. Their recorded reason is “the calculation would show fields the policy withholds”. The replay computes these divergences from the captured statuses. Field-removal checks are separate from claim correctness and do not establish that every financial amount is secret; top-level facts retain totals.

The retained usage estimate totals **USD 0.281259**, based on repository pricing assumptions. Model per-request USD remains unknown in the scorer output; it is not replaced by a false zero. Rules record zero model usage. This capture has no executed probe outcomes; zero probes means not run, not passed.

Public capture:

- [Agents predictions](public_rehearsal_c6ea270/agents.jsonl), [one-pass predictions](public_rehearsal_c6ea270/one_pass.jsonl), [rules predictions](public_rehearsal_c6ea270/rules.jsonl).
- [Worker metadata](public_rehearsal_c6ea270/run.json), [retained usage summary](public_rehearsal_c6ea270/usage_summary.json), [probe rows](public_rehearsal_c6ea270/probes.jsonl).
- [Policy control](public_rehearsal_c6ea270/policy-control.json), [policy restriction](public_rehearsal_c6ea270/policy-restriction.json).

Detailed external worker logs and raw outputs remain with the evaluator's original public capture; this replay uses the committed subset linked above.

## Four-worker parallel capture

A second public capture verified the newly integrated parallel harness before unseen evaluation. It used the same frozen candidate and public cases, with four workers and the pinned per-system timeouts. Both captures are retained and reported separately; no best-run selection or pooling is performed.

| Result | agents | one_pass | rules |
| --- | ---: | ---: | ---: |
| Official safe-complete cases | 0/2 | 0/2 | 1/2 |
| Worker jobs attempted | 2 | 2 | 2 |
| Jobs with an RFC failure | 2 | 0 | 0 |

All six scheduled jobs returned; the scheduler recorded zero exceptions, zero unstarted jobs, no rate-limit reduction and no deadline abort. All 12 retained prediction envelopes validate. Two agent RFCs, `rfc-0001-01` and `rfc-0002-01`, explicitly record `invalid_output` from the challenger. The preserved public replay for each records `SchemaValidationError: $.trace[1]: longer than 200`. This is a candidate internal-output schema failure, distinct from invalid captured prediction envelopes. No timeout or retry occurred. The two failures remain part of the second capture's score.

The replay command computes `parallel_rfc_invalid_output_count=2`, `parallel_rfc_timeout_count=0`, schema counts and scheduler/worker counts directly from committed outputs. The detailed challenger error text above was inspected in external public `public-parallel-c6ea270/capture-workers/agents-{1,2}/replay.json`; raw model logs are not needed by the replay command and are not copied into the repository.

Parallel capture: [agents](public_parallel_c6ea270/agents.jsonl), [one-pass](public_parallel_c6ea270/one_pass.jsonl), [rules](public_parallel_c6ea270/rules.jsonl), [run metadata](public_parallel_c6ea270/run.json), [scheduling](public_parallel_c6ea270/scheduling.json), [original scores](public_parallel_c6ea270/scores.json), [probe rows](public_parallel_c6ea270/probes.jsonl).
