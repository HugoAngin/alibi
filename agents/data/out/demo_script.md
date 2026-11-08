# Demo script: four minutes on case 1

**Handed to execution (Mouaad) at 11:25Z.** Dry run 2 (11:24Z, replay, clean `--reset`) was stopped after D2. The T−10 checks, 0:00, D1, the 0:25 screens and D2 were re-checked in that run. The 0:45–3:10 beats are from QA pass 2 and the 11:15 run, where each step was run on its own; they have not yet been timed end to end. Still to do: two timed dry runs, then the backup video.

Owner: data (Hugo). Final for the S5 build (`sealed-candidate`, main after `649b292`). Every command, URL, button and screen below was run by data on 2026-11-08 between 10:17 and 11:30Z (`QA_0430.md` pass 2, `fresh_clone_1115.md`). The Jinja app on 8100 is the screen. The phone holder app and the bank console run on the same backend once research and execution have wired them. Their routes go in the last section when they are verified.

## Roles

| Role | Who | Does |
| --- | --- | --- |
| Presenter | Arnav (lead) | Speaks every line, never touches the keyboard. |
| Driver | Hugo (data) | Runs the three Driver commands from a hidden terminal and clicks in three browser windows. |
| Backup | Elio (research) | Calls the switch to replay at T−5 and owns the results slide at 3:10. |

## Before going on stage (T−10 min)

1. **Start the stack** from the repo root, in a terminal the judges cannot see:

   ```bash
   ALIBI_MODE=agents uv run --frozen python -m alibi.demo --timeout 900 --case 1 --reset
   ```

   For the replay fallback, add `--replay` (see Fallback). **`ALIBI_MODE=agents` is required in both cases.** Without it the app runs the rules engine and every Gemini role shows "skipped".

   Wait for the one-line JSON. It must contain `"passport_seed": "connectors"` and `"merchant_pushed": 19`.

2. **Check the state:** `uv run --frozen python -m alibi.demo status`. The app must show `"mode": "agents"` and `"seed": "connectors"`, and both services `"as_of": "2026-10-25T00:00:00Z"`.

3. **Live Gemini only:** `uv run --frozen python -m alibi.llm.probe` makes 2–4 real calls (ask Hugo first). If it fails, or answers slowly, restart with `--replay` and tell the Presenter.

4. **Open three browser windows.** They must use separate cookies: a bank login on the same host blocks the holder pages with `403 recipient_credential`.

   | Window | URL | Notes |
   | --- | --- | --- |
   | Holder (phone width, 390 px) | `http://localhost:8100/passport` | Note: `localhost`, not `127.0.0.1`. |
   | Bank A | `http://127.0.0.1:8100/bank/bank_a/inbox?token=<bank_a>` | The token is the `bank_a` entry of `data/alibi/bank_tokens.json`. Open it once; after that the bare URL works. |
   | Bank B | the same with `bank_b`, in a **private window** | |

5. Bookmark `http://localhost:8100/sources/rec-0001-020/1` in the holder window. Nothing in the app links to the release record before Run, and the page returns 404 until D1.
6. Put the results slide on the deck laptop (3:10).

**Expiry.** `rfc-0001-01` expires **2026-11-09 09:00Z**. After that this script no longer works as written; regenerate the fixtures (`ALIBI_NOW` freezes the app clock and breaks the approval window).

## The three Driver commands

```bash
# D1 at 0:25: the release message, the £2,160 credit and the alert arrive through the connectors
uv run --frozen python -m alibi.demo advance --to 2026-10-28T07:30:00Z

# D2 at 0:45: move both services past real time (needed before the first Send), then Bank A's request
uv run --frozen python -m alibi.demo advance --to $(date -u -v+15M +%Y-%m-%dT%H:%M:00Z)   # Linux: date -u -d '+15 min' +%Y-%m-%dT%H:%M:00Z
uv run --frozen python -m alibi.demo rfc bank_a

# D3 at 2:45: Bank B's request
uv run --frozen python -m alibi.demo rfc bank_b
```

Each takes about 1 second and prints one JSON line. `rfc` must print `"forwarded": {"status": "accepted", …}`.

**Why D2 has a clock step.** The app runs on the wall clock, while the services run on the demo clock. If Bank A's service clock is behind the package's issue time, it refuses the package with "the package was issued in the future", and that package ID can never be sent again in this run (QA pass 2, P0-A). Never skip the `advance` in D2.

## The run

| Time | Window, URL | Driver does | Must be on screen | Presenter says |
| --- | --- | --- | --- | --- |
| 0:00–0:25 | Holder `/passport` | Nothing. | Maya, `maya-001`. Connections: Mock Marketplace (issuer verified) and Bank A account feed (bank observed). Rent −£650.00, grocery −£12.40 and pharmacy −£23.50 in Transactions. No alerts, no requests. | "This is Maya. She resells clothes online. Alibi keeps her evidence as it arrives: signed records from the marketplace, and her bank feed." |
| 0:25–0:45 | Holder `/passport`, then `/sources/rec-0001-020/1` | **D1**, then reload. Open the alert, then the release record. | Alert **Unexplained credit**, open, raised before any bank has asked: "Credit txn-0001-004 … has no settlement record in the passport that names its reference and matches its amount and timing." The credit **£2,160.00** `MOCK MARKETPLACE PAYOUT PO-0001-01`, **Unmatched**. Record `rec-0001-020`, issuer verified: "£2,160.00 from your recent sales is now available in your **seller balance**. Withdrawal PO-0001-01 … has been requested." | "£2,160 just landed in her bank. Alibi noticed on its own: nothing in her passport proves where it came from. The marketplace did send a signed message, but read it: the money reached her seller balance. That is not the same as reaching her bank." |
| 0:45–1:25 | Holder `/requests/rfc-0001-01` | **D2**, reload the passport, open the request under Requests, click **Run investigation**. | Request from Bank A, signature **verified key bank_a**, transaction-scoped `txn-0001-004`, expires 2026-11-09 09:00Z. Questions `q.source_of_funds` and `q.breakdown`. After Run, the roles table reads Detective **complete 4**, Challenger **complete 3**, Curator **complete 2**, and a "code verdict" row with lookups 1. | "Bank A has restricted her account and asks where the money came from. Gemini agents investigate: one proposes an answer, one challenges it, one prepares what to share. The models only propose. Code checks every reference and amount and decides the result." |
| 1:25–1:50 | Same page, Trace | Scroll to the trace for `q.source_of_funds`. | LOOKUPS: `lookup_settlement payment_ref=PO-0001-01 → found=True … record_id=rec-0001-021 … status=settled`. Source `rec-0001-021` r1 **Issuer verified via lookup**. CALCULATION `240000 - 18000 - 6000 = 216000 → £2,160.00`, computed by code. Both questions **✓ Supported**. | "The agents did not accept a matching amount. They asked the marketplace for payout PO-0001-01 itself, and it came back settled on 27 October, before the credit was booked. £2,400 of sales, less £180 of refunds and £60 of fees, is exactly £2,160, and code recomputed that from the cited records. Only now is the answer supported." |
| 1:50–2:25 | Same page, Private review and Approval; then the dispatch page | Show Included. Click **Approve for Bank A**, then **Send to Bank A**. | 20 Included: the 18 sales, refunds and fees, plus payout `rec-0001-021`. Rent, grocery, pharmacy and her own purchase are **not** in the list. The approval names **Bank A** with the body digest. Then **Delivered to Bank A**, with a package ID and a receipt with status **accepted**. | "Maya sees exactly what Bank A will receive: her sales, refunds, fees and the payout. Her rent, her groceries, her pharmacy purchase and what she bought herself are not in it. She approves this exact version for this bank, and sends." |
| 2:25–2:45 | Bank A window, inbox, then the package | Reload the inbox, open the package, then under Disposition pick **Clear**, type a short reason and click **Record disposition**. | Inbox: one package, authentication **valid**, Supported 2. Package page: three separate panels. **Package authentication** has 7 checks, all pass. **Source trust** shows each attachment issuer verified, with issuer signature pass. **Claim status** is Supported. Then "Disposition recorded: clear. No account action was taken." | "Bank A checks the package. A valid signature only proves it came from Alibi unaltered. Who stands behind each record, and whether each answer is supported, are shown separately. A human analyst records the decision; nothing clears her account automatically." |
| 2:45–3:10 | Holder `/requests/rfc-0001-02`, then the Bank B window, then the Bank A window | **D3**. Open the request, click **Run investigation**, **Approve for Bank B**, **Send to Bank B**. Reload Bank B's inbox. Copy Bank B's package URL and paste it into the **Bank A** window. | Request from Bank B, period `[2026-10-01, 2026-11-01)`, one question. **Supported**: sales **£2,400.00 from 8 records**, refunds **£180.00 from 2 records**, the **same record IDs** as Bank A's (`rec-0001-001` …). 10 Included: no payout, no fees. Delivered to Bank B; its inbox lists it, Bank A's does not. In the Bank A window, Bank B's URL is **Refused, recipient_mismatch**. | "A second bank asks a different question: her October sales and refunds. Same passport, same record IDs, nothing re-imported. A smaller package, and a fresh approval for Bank B. And Bank A cannot open Bank B's package, even with the link." |
| 3:10–4:00 | Deck: results slide | Switch to the deck. | Eval's verified numbers only. | Read the numbers as they are on the slide. Close: "Reusable evidence, controlled disclosure. Whether real banks would accept this is unmeasured." |

Timing note. In the 11:2xZ dry run each Driver command took about 1 s and each Run about 3 s in replay. A live Gemini run takes longer; if the 0:45 beat runs over, cut the 1:25 line to its last two sentences.

## Do not do on stage

- **Do not Remove an included item after approving.** Every included record is cited, so removing one turns both of Bank A's answers into Unresolved ("withheld by holder"). There is no Restore button, and running again keeps the removal. To show that an edit voids the approval, rehearse it first; the screen then says "The body changed, so any earlier approval no longer matches", and Send disappears.
- **Do not show a follow-up request from the Jinja app.** The holder-side proxy route is a known limit (lead 10:52Z). A follow-up is shown only on the bank console (native route). If the console is down, skip that beat.
- Do not call a claim "verified" or "true". Say **supported**, **contradicted** or **unresolved**, as the badge says.
- Do not say the bank accepted anything or cleared the account. The disposition is a human record.
- Do not say Gemini or the app cannot read the data.
- Do not show the terminal, the tokens file or dev tools.
- Never present replay output as live.

## Fallback: replay (Gemini down or slow)

- Backup decides by T−5. Restart with:

  ```bash
  ALIBI_MODE=agents uv run --frozen python -m alibi.demo --timeout 900 --case 1 --reset --replay
  ```

- Every page shows the **REPLAY · Recorded run** banner. Do not hide it.
- The agents' answers come from `alibi/fixtures/replays/case1_agents.json` (Detective 4, Challenger 3, Curator 2, 0 live calls). The connectors, the feed, request intake, signature checks, the approval digest, dispatch, the bank's verification and every refusal still run live.
- The clicks, Driver commands and screens are the same as above.
- Presenter adds at 0:45: "Gemini is not reachable from this room, so the agents' answers are a clearly labelled recording of the same case. The connectors, signatures, checks and refusals you see are running live."
  - The banner text says "not live agent or connector activity", which is wrong about the connectors (QA pass 2, item I). Say the sentence above anyway, because it is accurate.
- If the app itself fails, play the backup video.

## Case 2 (contradiction), only if asked

`--case 2` gives the same story with payout `PO-0002-01` still **pending** a day after the credit, so the settlement claim is **Contradicted** and the origin stays **Unresolved** (`case2_walkthrough.md`). **Not rehearsed on S5.** There is no case 2 replay recording, so it needs live Gemini. S5 also notes that the rules engine does not reach the pending payout on case 2. Run it only if lead has rehearsed it.

## Phone holder app and bank console

Lead's plan is the phone holder app (research) plus the bank console (execution) on this backend, with the Jinja app as the fallback. When those are wired to S5, the marks, Driver commands and expected states above stay the same; only the windows change. Research and execution add the verified URLs here. Data checked at 10:27Z: the deployed `/simulate/` pages are a fixture simulation that disagrees with the backend (QA pass 2, item D), and the deployed bank console needs a passcode data does not have. Neither is ready to replace the Jinja windows yet.
