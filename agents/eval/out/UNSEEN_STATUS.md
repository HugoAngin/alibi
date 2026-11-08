# Unseen evaluation status

Owner: Verify unseen data workflow chat. The master coordination chat handles public integration and probe evidence. Only this evaluator chat launches the unseen capture.

At 10:58 UTC on 8 November, lead announced `sealed-candidate-2` at `9468c20`, adding typed facts to both Gemini modes, and extended the hard stop to 12:50 UTC. At 11:00 the remote tag was still absent; eval requested the tag push on board comment 6720649301. Once verified, rerun public Cases 1 and 2 and bind fresh public probe evidence to that candidate. No release manifest or capture lock has yet been created.

The original candidate public results and both sequential and parallel rehearsals are documented in `PUBLIC_REHEARSAL.md`. Validation record `b9f980d5e6` passed clean-checkout replay for the first public report. The integrated four-worker check retained two challenger schema failures, with no timeout, retry or infrastructure error. Scored application source remains unchanged.

The runner uses four disposable workers, caps agents/one-pass/rules at 180/90/30 seconds, reduces concurrency to two on retry evidence, and stops at 12:50 UTC. Infrastructure exceptions stop new launches; missing outcomes stay in the denominator. Capture locks prevent duplicate execution and evidence hashes bind scoring replay. Retained usage and post-policy claim divergence are reported separately from investigation correctness.

Before opening: update the configuration for the exact candidate, verify the public rehearsal and candidate probe receipt, write and verify the private manifest, then invoke the authorized `tools/results.py add --split lockbox` once. Never set the lockbox flag manually. Post start and 11:15 progress on the Message Board; verify the saved record without new model calls and publish aggregates only.
