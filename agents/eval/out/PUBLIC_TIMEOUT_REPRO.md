# Public reproduction of the sealed scheduler interruption

Candidate `58bf0d4a199f5ad3833fa9bde751d31140219975`, original evaluator `30758fc`, macOS, four scheduler workers. Public Case 1–12 inputs ran in rules mode with a fake credential, separate scratch lanes, the actual isolated worker and scheduler, and child stderr captured to disposable files. The public test used a deliberately shortened 0.25 s rules timeout to reach cleanup quickly. The sealed benchmark's limits are separate.

Once the first child launched, the test waited 75 ms, suspended only the coordinator, waited 1.25 s, then continued it. The original evaluator returned four real `killpg` EPERM errors, which became four `PermissionError` scheduler errors and halted dispatch after four jobs. The tested reaping change produced four ESRCH and eight successful signals, dispatched all 12 jobs, and recorded 14 typed RFC timeouts without a scheduler error. A separate set of 36 forced timeouts with each evaluator and no coordinator pause produced no `PermissionError`.

This public test reproduced the late scheduler stop. The earlier generic child exits remain a separate diagnosis. The test used no sealed data, no gold and no live model calls. A synthetic EPERM with an exited leader and a live descendant holding stdout also showed why the drain must stay bounded when group cleanup cannot be confirmed.

Run from the repo root using a trusted local evaluator revision. `--evaluator-ref` selects the worker source to exercise. Both variants use the same frozen candidate, public fixtures, scheduler and lane isolation. Expect the original macOS failure when the coordinator pauses over the child exit window. Timing and the number of affected jobs can vary across machines.

```bash
uv run --frozen python -m agents.eval.public_timeout_repro --evaluator-ref 30758fc
uv run --frozen python -m agents.eval.public_timeout_repro --evaluator-ref <harness-fix-commit>
```
