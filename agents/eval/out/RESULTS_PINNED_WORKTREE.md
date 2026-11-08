# Pin the checkout for future lockbox captures

The recorder currently runs against the working checkout and reads HEAD after
the command exits. A concurrent pull can therefore attach a different evaluator
revision to a retained run, even when the capture itself correctly pins the
evaluator that launched it.

`RESULTS_PINNED_WORKTREE.patch` changes only the lead-owned results recorder and
its test. Lockbox additions resolve HEAD once and run in the same kind of
detached worktree that verification uses. The recorder checks the split in the
detached checkout, writes the result JSONL to the initiating checkout and
removes the temporary worktree. Keep retained capture outputs at absolute
external paths. Other splits use their existing working-checkout behavior.

From the repository root, the owner can check and test the patch with

```bash
git apply --check agents/eval/out/RESULTS_PINNED_WORKTREE.patch
git apply agents/eval/out/RESULTS_PINNED_WORKTREE.patch
uv run --frozen python -m pytest tools/test_results.py
```

All 15 tests passed independently on a disposable archive of `181d73e`. The
new test advances the initiating branch during a synthetic lockbox command.
The old recorder stamps the later SHA. With the patch, the synthetic command
reads the launch checkout, the record carries its SHA and the worktree is
removed. Existing capture artifacts and recorder verdicts are left intact.
