# Issuer-text lookup safety follow-on

Apply after the forthcoming complete issuer-text `RETRIEVAL_ACCURACY.patch`, not after the earlier batch-only snapshot. This diff was prepared against a private copy of `/tmp/retr-eval-work/alibi/core/investigate_rules.py`, source SHA256 `dc8b04779f783ff67864bd1a73f001f9bcb25e798ac8cf684b6182ddcb559d5b`. Neither the Opus worker's source nor shared native files were edited. Keep the separate batch dependency/allocation patch as well.

The new waiting-payout gap used a pending lookup record without retaining its revision as a claim dependency. Signed clues used to justify supported retrieval were also omitted from dependencies. This patch records the selected signed clues and accepted lookup records in a run-private map, then adds those revisions to `Claim.examined_refs` for source and breakdown questions regardless of verdict. Clues are retained even when their lookup returns nothing: a corrected reference can change the conclusion. Existing source-revision, stale-draft and consent checks therefore apply. The clue and waiting records are not added as recipient citations or attachments.

Pending wording now says that the available records do not establish settlement by booking. An initial pending observation before booking does not prove the payout remained pending at booking.

Four focused public tests: pending payout correction to settled; correction of the clue behind an unresolved result; correction of the clue behind a supported result; and an unsigned imported message naming a novel reference. The first three fail on the copied pre-patch source and pass after this patch. Each correction invalidates the claims, draft and approval; pending packages stay empty and clue IDs stay out of the recipient body. The unsigned-text control refuses before invoking the connector, both before and after this patch.

Focused new, unresolved-dependency and gateway suites: **32 passed**, no failures or xfails (2.59 seconds). No models, private cases, gold edits, scorer changes, gateway permission changes or full-suite run.

```sh
uv run --frozen python -m pytest -q alibi/tests/test_text_lookup_safety.py alibi/tests/test_unresolved_dependencies.py alibi/tests/test_gateway.py
```

The existing retrieval function accepts only this subject's issuer-verified message records and checks that a returned verified payout has the same issuer as its naming clues. Imported text still cannot open lookup scope. This patch does not broaden that policy or promote source trust. It preserves actually chosen text/read dependencies, not every possible unseen clue or later source arrival.

Integration note: the answer-semantics worker also changes rules answers. Preserve the new `text_dependencies` merge into `Claim.examined_refs` when combining; supported answers need these private dependencies too. The usual package scrubber uses them as private context, while public citations and attachments remain minimal.

Patch SHA256: `5d7d6dd9d7382c8708e46fcf9ea60a7475f1ae585f349542dae02a83f4f4ae90`.
Tested scratch: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-text-safety-e4vj17az`.
