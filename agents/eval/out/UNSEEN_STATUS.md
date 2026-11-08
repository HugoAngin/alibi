# Unseen evaluation status

Capture finished at 11:49:59 UTC on 8 November 2026. Candidate remains `58bf0d4`, variants 3. The original interrupted record `1b235c16e9`, authorised continuation `e16afeeac5`, and aggregate-only export `f8dc5763b6` all passed retained-evidence replay.

All 132 distinct case/system groups have outcomes. Each system completed 10/44 cases without a worker failure and failed 34/44; official and second scorers both report 0/32 main cases safe-complete, and official adversarial scoring reports 0/12 for each system. There is no demonstrated comparative advantage. See `UNSEEN_RESULTS.md` and `UNSEEN_AGGREGATES.json` for denominators, partial costs and limitations.

The first attempt had 70 attempted, 67 returned, three scheduler PermissionErrors and 62 unstarted. Under lead rules 7/8, the continuation preserved all 67 returned groups and ran only 62 unstarted plus three publicly reproduced infrastructure errors. All 65 continuation jobs returned with zero scheduler exceptions. The manual operator pause, cleanup race, bounded cleanup fix and affected timing are disclosed in `PERMISSION_FIX.md`. Earlier generic child exits remain unexplained and counted.

The requested 12:05 checkpoint is superseded by this completed capture, finished before that time. The authorised 12:50 stop was not reached. No further hidden model runs are authorised by this report. Candidate, configuration and original capture lock are unchanged; raw sealed evidence stays private.

Owner: Verify unseen data workflow chat. Master coordination owns public demo/integration, with a separate Claude agent assisting the bank-request diagnosis. Lead closed sealed work at 12:02: no mapping or rerun. Post-hoc public Case 39 reproduces the two observed import-schema crashes; known observed ingest failures are17/18/18 by agents/one-pass/rules, with remaining causes unknown. Current assignment is the pre-submit checklist and README-cited result verification by12:20. Any deployment fixes remain separate from the frozen comparison.
