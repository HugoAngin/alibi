# Pre-submit checks, 8 November 2026, 12:18 UTC

Submission is not yet ready: repository publication, final root README, Devfolio links,
video playback, roster and prize selection remain with their owners.

| Check | Result | Evidence / next action |
| --- | --- | --- |
| Frozen records cited by research draft | PASS | `1b235c16e9`, `e16afeeac5`, `f8dc5763b6` replayed. The first needed its original external-path environment restored; an initial missing-environment error is retained, followed by PASS. No candidate execution on replay. |
| Receipt vision record cited by research draft | PASS | `73e742c7d7` independently reran its three public receipt calls and matched its recorded metrics. |
| Post-fix public comparison | PASS | `6d9da33c2f`: rules safe-complete stays 35/80 before/after import resilience; zero worker failures in both public runs. |
| Post-fix reused-pack runtime diagnostic | PASS, development only | `09a90f6cd0`: 44/44 rules cases complete without worker failure; no model calls or gold scoring. This is an additional disclosed look, not fresh unseen evidence. |
| Multiple lockbox records disclosed | Needs final README update | Original interrupted capture and authorised continuation are preserved. Tamim subsequently authorised a separate post-hoc diagnostic. The final write-up must distinguish that additional development run. |
| History credential pattern scan | PASS for specified scan | Full `git log -p --all`: ten matching lines reviewed as regexes, instructions or synthetic fixtures; no credential-shaped full matches. This is not a guarantee against every secret format. |
| Message Board review | PASS for credential patterns; latest messages skimmed | 457 comments scanned, zero credential-shaped matches. No hidden cases/gold exported by this diagnostic work. |
| Tooling unit tests | PASS | 59 tests. |
| Application suite after import fix | FAIL | 1,374 passed, two failed: same-request-ID cross-bank approval and demo port-release smoke. Isolated rerun passed the bank test but still failed the smoke port assertion. Both reported to owner; timing nondeterminism and TIME_WAIT under investigation. |
| Repository visibility / logged-out access | PENDING owner | Repository PRIVATE; logged-out GET returns 404. Lead assigns publication to Elio at submission. |
| Root README | PENDING owner | Still team setup placeholder. Research draft now corrects private calibration and 80-case data scope; lead/Arnav must publish the completed write-up. |
| Devfolio and video | PENDING owner | Final URLs not supplied; cannot verify logged-out rendering/playback. |
| Roster / tracks / prizes | PENDING owner | Lead says pending from Arnav. |
| Deadline stop | SCHEDULED constraint | Lead evaluation hard stop is 12:50 UTC; submission is 13:30. No claim this future check has passed. |

Private verification logs are retained outside the repository under
`/Users/tamim/Documents/Codex/2026-11-08/alibi-unseen-evaluation/presubmit/`.
The frozen negative result remains the submitted benchmark. Ongoing user-authorised fixes
and reused-input diagnostics are separately labelled development work.
