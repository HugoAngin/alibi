# S5 two-bank Worker rehearsal

On 8 November at 13:32 UTC, a fresh synthetic Case 1 stack and all three
loopback Astro Workers ran in rules mode. All 418 tracked Alibi and web files
in the rehearsal matched `91b5fbaf1382b4eac12369872fc6d1c9711a71db`.
The browser used the local Maya session and local analyst sign-in. The
developer browser reported missing Clerk client keys, while the local holder
session and all sharing actions completed.

1. The marketplace Withdraw button advanced the shared clock and flagged the
   credit. The analyst signed in, opened FC-0147 and sent Bank A's RFC.
2. Maya signed in and ran, approved and sent `rfc-0001-01` with separate
   browser actions. The holder showed an accepted receipt and Bank A's
   signed-in inbox showed valid authentication.
3. The authenticated local director issued `rfc-0001-02` for Bank B. Maya
   again ran, approved and sent it from the same persisted passport.
4. Both receipt IDs in the holder UI matched the recipients' receipts. The
   independent verifier checked both raw envelopes with the pinned local
   public key registry. Each bank held exactly one verified receipt and got
   HTTP 404 for the other's package.
5. Bank A's analyst recorded Clear with a reason. Both persisted on reload.

For the staged run, use `alibi/RUNBOOK.md` and `web/scripts/demo.mjs`.
The director's authenticated `POST /demo/request` accepts a JSON body with
`recipient` set to `bank_b`. The existing bank console only issues Bank A's
request. See `agents/eval/CASE_FORMAT.md` for the independent receipt gate.

The [candidate 5 routed receipt gate](s5-3c50476-routed-handoff.json) was run
separately on the exact tag using the pinned local public registry. It
confirmed two verified receipts, anonymous approve and dispatch refusals
with HTTP 401, bank bearer refusals with HTTP 403, and cross-bank reads
with HTTP 404.

At 13:33 UTC, all three hosted Workers reported `received` at version
`908586d42cefb30f`. That director stage derives from Bank A's verified
inbox. The hosted Bank B holder request returned HTTP 404, so its hosted
receipt still needs a separate run and readback.
