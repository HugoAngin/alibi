# Risk controls and oversight (15%)

Owner: `execution`. Source for the risk-controls slide and the README limits section. Maps each threat row in `docs/SPEC.md` (Threat controls) to its control, the probes that test it ([`PROBES.md`](PROBES.md)) and where the evidence will be.

**Status legend.** ✅ on `main` now. ⏳ planned: the path is owned by a build session and not on `main` yet. Lead replaces ⏳ with real paths and the eval result before the deck freezes. **No number goes on the slide until eval's `results.py verify` passes it.**

## Slide

**Title:** Code decides what leaves. Humans decide what it means.

| Threat | Control (enforced in code, not prompts) | Tested by | Evidence |
| --- | --- | --- | --- |
| **Fabricated source, arithmetic or conclusion** | Every cited record ID and revision is checked against what was retrieved. Money is recomputed in pence from cited amounts by code, and again by the bank's verifier. A later payout cannot explain an earlier credit. Contradicting evidence is kept. | SP-41, SP-42, SP-43, INJ-02, INJ-07, INJ-21 | ⏳ trace view on the request page (S4); ⏳ verifier recompute (S2 `alibi/bank/`); ✅ timing and matching rules in `shared/CONTRACTS.md` |
| **Malicious text in a receipt, request or tool result** | All text is data. Agents can only propose schema-validated output. Tools are allowlisted and scoped to Maya's subject. Out-of-scope requests are refused and logged. | SP-38, SP-39, SP-40, SP-45, all 25 in `injections.jsonl` | ✅ [`injections.jsonl`](injections.jsonl); ⏳ gateway refusal in the audit log (`tool.refused`); ⏳ eval injection run |
| **Over-disclosure through fields, summaries or traces** | Plain-English rules compile to typed deny, generalise, age and recipient rules, and Maya confirms them. Ambiguous rules are held and deny wins. The package is built from allowlisted fields and the policy applies to every rendering: body, summaries, extracts, metadata and PDF. Gaps never name what was withheld. The exclusion list stays on Maya's side. | SP-10 to SP-16, PR-01 to PR-40 | ✅ [`policy_rules.jsonl`](policy_rules.jsonl); ⏳ private review screen (S4); ⏳ eval `forbidden_items` / `unnecessary_items` |
| **Forged sender, tampering, replay or stale approval** | Approval is bound to the SHA-256 body digest, recipient, request, policy version and cited source revisions, and expires after 15 minutes. Dispatch rechecks all of them and refuses on any change. Ed25519 signatures with separate merchant and Alibi keys. Event and package IDs are deduplicated. | SP-01 to SP-09, SP-25 to SP-28, SP-31, SP-33 to SP-35, C1 to C16 | ✅ [`consent_matrix.md`](consent_matrix.md); ⏳ live refusal on stage (demo 1:50); ⏳ `dispatch.refused` audit entry |
| **Unauthorised vault access or data leakage** | Holder and analyst roles are separate. Recipient scope comes only from the credential, on every inbox, package, attachment and receipt endpoint. Bank A cannot list or fetch Bank B's package, and a wrong ID gets a 404 without revealing whether it exists. Evidence is encrypted at rest with an app key stored apart from the data. Gemini receives only the fields its task needs. | SP-17 to SP-24, SP-46, SP-47 | ⏳ isolation test (S2 / eval); ⏳ demo 2:45 (Bank B inbox) |
| **Tool failure, missing evidence or misuse of alerts** | Retries are bounded. Unavailable evidence becomes a typed `unresolved` reason, never a made-up success. Alerts describe observed facts and verification gaps, never suspicion or profiling. Agents cannot sign, approve, send or act on an account. | SP-44, SP-45 | ⏳ unresolved gaps in eval results; ✅ alert and disposition wording in [`ANALYST_FLOW.md`](ANALYST_FLOW.md) |

**Human oversight, two decisions:**
- **Maya** approves the exact package for one named bank. Any change after that needs a new approval.
- **The analyst** records clear, escalate or request more, with a reason. Nothing changes on any account automatically.

**Footer number:** `probes_blocked / probes_attempted` and `controls_allowed / controls_attempted` from `agents.execution.api.summarise`, as verified by eval. ⏳

## Speaker notes (about 25 seconds)

1. "Every control on this slide is enforced in code. The agents can only propose."
2. Point to the live refusal from the demo: the package changed after approval, so dispatch was refused, and the reason was shown word for word.
3. "Signed doesn't mean true." The bank sees package authentication, source trust and claim status as three separate checks.
4. Read the footer number and name one probe that failed, if any did.

## README limits section (draft)

- Both connections are simulated services with synthetic data. No real bank, merchant, account or money is involved.
- The app holds the encryption key, and Gemini processes the synthetic fields sent to it. We do not claim that the app or the inference provider cannot read data sent for processing.
- Probes run against our own mock services. They show our controls work as built, not that a real bank integration would be secure.
- The policy compiler supports five rule kinds. Absolute dates, aggregation ("refunds as a total"), rules about people and standing auto-share permissions become clarifications rather than rules (PR-20, PR-26, PR-28, PR-31).
- Monitoring covers observable discrepancies and verification gaps. It is not AML screening or scam detection.
- Signing proves integrity and which key signed. It does not prove a claim is true; the analyst decides that.
- Real bank acceptance, and any reduction in account restrictions, are unmeasured.
- Production would need separate privacy, security and regulatory review, real key management and a holder-controlled vault.
- ⏳ Any probe that failed in the frozen run, named here with its ID.
