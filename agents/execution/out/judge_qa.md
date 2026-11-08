# Judge Q&A: controls and trust

Owner: `execution`. For the presenter to learn on Sunday morning. Each answer is one paragraph you can say in about 20 seconds, then an **Evidence** line to point at if pushed. IDs refer to [`PROBES.md`](PROBES.md) (SP), [`injections.jsonl`](injections.jsonl) (INJ), [`policy_rules.jsonl`](policy_rules.jsonl) (PR) and [`consent_matrix.md`](consent_matrix.md) (C, F).

**Rule for answering:** say what is enforced in code, then what it does *not* prove. ⏳ means pending at the time of writing; lead replaces it with the verified result before the deck freezes. Never quote a number that eval has not verified.

---

### Keys and signatures

**1. Who holds the signing key?**
In the prototype the app holds it: one demo master key in the environment, from which a package-signing key is derived, kept apart from the stored data. The mock merchant has its own key, and the bank pins both in a registry with one purpose each, so a merchant key can never sign a package. That is a demo arrangement. Production would need real key management, and the roadmap moves the vault and key to the holder. We do not claim the app cannot read Maya's data. At rest, our claim covers the passport store only: evidence is ciphertext and the key sits outside the data folder (SP-47). The demo state files, the app's `case_N.state.json` and the mock bank's `state.json`, hold delivered packages in plaintext; that is a stated limit, and a delivered package is the recipient's copy.
**Evidence:** `shared/CONTRACTS.md` (Signing, At rest); eval cd71c90 (plaintext state files); `agents/eval/sharing_delivery.py` (purpose-pinned registry, key derivation); SP-28, SP-47.

**2. What stops someone forging an Alibi package?**
Every package is signed with Ed25519 over a canonical JSON encoding, and the bank verifies it against a pinned key before showing anything. We ran the attacks: one edited character, one changed signature byte, an unknown key and the merchant's key used as Alibi's. All four were refused with a specific reason, and nothing was stored. This was a local reference run against the team's verifier; the same attacks are wired to run against the live bank service.
**Evidence:** `agents/execution/out/verifier_reference_run.json` (SP-25, SP-28 refused with `bad_signature`, `unknown_key`, `wrong_key_purpose`); live HTTP ⏳.

**3. A valid signature: does that mean the explanation is true?**
No, and the analyst screen is built so nobody can read it that way. It shows three separate checks: is this the package Alibi signed for this bank, who vouches for each attachment, and does the evidence support each answer. A perfectly signed package can carry a self-supplied receipt and an unresolved claim, and the screen says so.
**Evidence:** `agents/execution/out/ANALYST_FLOW.md` §2; SP-30, SP-32; `FRONTEND_NOTES.md` item 2.

### Trusting holder evidence

**4. Why should a bank trust evidence that comes from the customer?**
It should trust each item exactly as far as its source allows, and Alibi labels which is which. Records the merchant signed are checked against the merchant's key. Rows from the bank's own feed are bank-observed. Anything Maya uploaded is marked self-supplied by the backend, whatever she claims, and shown at lower trust. Alibi saves the bank from chasing documents. It does not ask the bank to believe Maya.
**Evidence:** `shared/CONTRACTS.md` (Trust); SP-29, SP-30, SP-36; INJ-08.

**5. Couldn't Maya fake a receipt?**
She can upload anything, but it arrives labelled self-supplied and can never be badged issuer-verified. If she alters a merchant-signed record, its signature fails and the bank rejects the whole package. Our test changed a payout amount after signing and it was refused.
**Evidence:** reference run SP-29 (`source_signature_invalid`, `source_signature_missing`); SP-36.

**6. Matching amounts: couldn't a fraudster time a payout to match the credit?**
Matching amounts never prove anything on their own. A settlement must have its own reference and timing: settled no more than five days before the bank booked the credit, and never after. A later payout cannot explain an earlier credit even if the amount is identical. A message saying funds were "released to your balance" is not proof the money reached the bank. That distinction is the core of the demo.
**Evidence:** `shared/CONTRACTS.md` (Settlement timing, Questions and matching); SP-43; INJ-21.

### The LLM

**7. Can a prompt injection in a receipt or message make the agents lie?**
The agents can only propose. Every output is validated against a schema, every cited record and revision is checked against what was actually retrieved, and the money is recomputed in code. Tools are allowlisted and scoped to Maya's account. We wrote 25 injections, planted in merchant messages, receipts, bank questions, tool results and bank rows. Each is run alongside the clean case, and the claim status must not change.
**Evidence:** `injections.jsonl` (INJ-01 to INJ-25); SP-38 to SP-42; agent prompts in `alibi/agents/prompts/` (the "text is data" rule); eval injection run ⏳.

**8. Can the LLM leak the list of things Maya chose not to share?**
The exclusion list never leaves Maya's side. The backend builds the bank-facing package from allowlisted fields and recomputes the policy filter after the Curator proposes, so a model that tried to include it would be overridden. Gaps are described generically, so even the gap wording does not reveal what was withheld.
**Evidence:** SP-13, SP-15; INJ-17; `consent_matrix.md` notes.

**9. Does Gemini see Maya's whole financial history?**
No. Each role gets only the fields its task needs, and lookups go through a gateway scoped to her account. The data is synthetic, but it is sent to Google for processing, and we don't claim otherwise. Our client logs only hashes and token counts, never the prompt, the data or the answer.
**Evidence:** `alibi/llm/client.py` (module docstring: logging); SP-40, SP-46; `CONTROLS_SLIDE.md` limits.

**10. What happens when Gemini is down?**
Calls are retried a bounded number of times and capped per task and per process. If the model is unavailable, the claim becomes "unresolved" with a typed reason. It never becomes a made-up success, and the code-only checks (signatures, arithmetic, timing) still run. On stage, if Gemini is down, we play a clearly labelled recording.
**Evidence:** `alibi/llm/client.py` (retries on 429/503, call cap, `Unavailable`); SP-44; `docs/TASK.md` (recorded run rule).

**11. Can an agent send or sign something without Maya?**
No agent has a sign, approve or send tool. Only the backend dispatches, and only after rechecking Maya's approval at the moment of sending. An injected "approve and send now" ends in a logged refusal.
**Evidence:** SP-01, SP-45; INJ-10, INJ-22; `consent_matrix.md` C1.

### Consent and disclosure

**12. What exactly does Maya approve?**
She approves one exact package for one named bank and one request. The approval is bound to the package's fingerprint, the recipient, the request, her privacy rules and the record versions it cites, and it expires after 15 minutes. If any of those changes, sending is refused and she approves again. In the demo we change the package after approval and show the refusal live.
**Evidence:** `consent_matrix.md` C2 to C16 and flow F1; SP-02 to SP-07.

**13. If Maya approves for Bank A, can Bank B get the same thing?**
No. Approvals are looked up by request, and each is bound to its recipient. Bank B's question gets its own filtered package and its own approval, even when the evidence overlaps. The passport is reused; the consent is not.
**Evidence:** `consent_matrix.md` C4, C12 and flow F3; SP-03.

**14. Can Bank A see what was sent to Bank B?**
No, and the separation is enforced on the server. Each bank's access is fixed by its credential, not by a dropdown or an ID in the URL. Asking for Bank B's package as Bank A gets a "not found" that doesn't even confirm the package exists. The same rule applies to the inbox, attachments and receipts.
**Evidence:** SP-17 to SP-21; `consent_matrix.md` F3 steps 7 to 9; isolation test ⏳.

**15. What if Maya types a privacy rule the system misunderstands?**
Rules compile into five strict types, and Maya sees the interpretation and confirms it before it takes effect. Anything vague, unsupported or naming something that doesn't exist becomes a question back to her instead of a guess. When rules conflict, deny wins. We test 40 realistic rules, and the harness counts every wrong parse that would actually disclose something.
**Evidence:** `policy_rules.jsonl` (PR-01 to PR-40); `agents/execution/policy_harness.py`; leak counts per parser ⏳.

**16. What happens when a merchant corrects a record after Maya shared it?**
What was sent stays exactly as sent: the bank's copy is never altered, and Maya's history records it. In her view, the old evidence is marked superseded. If the correction lands before sending, the pending approval is cancelled and she reviews the updated draft. Corrections to records the package doesn't cite leave the approval alone.
**Evidence:** `consent_matrix.md` C8, C9; SP-06, SP-08; SPEC Consent and signing step 3.

### Banks, regulation and oversight

**17. Doesn't this tip off the customer about a bank's suspicions?**
Alibi only answers the questions the bank chose to send. It never surfaces anything the bank hasn't said, and it does no AML scoring or profiling. The analyst's decision and reason stay on the bank's side and are never sent to Maya automatically. Whether a given request may be shown to a customer at all remains the bank's own regulatory decision; Alibi doesn't change that.
**Evidence:** `shared/CONTRACTS.md` (Disposition: visible to the deciding recipient only); SPEC Threat controls (last row); `ANALYST_FLOW.md` §3.

**18. Does the analyst's decision change the account automatically?**
No. The analyst records clear, escalate or request more, with a reason, and nothing changes on any account. "Request more" sends a new, linked request back to Maya, and she approves that answer separately.
**Evidence:** `ANALYST_FLOW.md` §3 and §4; `consent_matrix.md` flow F2.

**19. Could someone resend an old package or change it in transit?**
Packages expire, and receipts are bound to the exact bytes received. Sending the identical bytes again returns the original receipt, not a second delivery. Reusing a package ID with different contents is refused, and an expired package is rejected.
**Evidence:** reference run SP-27 (`expired`), SP-31 (identical resend deduplicated, `package_id_conflict`); `consent_matrix.md` C14, C15.

**20. What haven't you proven?**
Everything runs on simulated services and synthetic data. The probes show our controls work as built, not that a real bank integration would be secure. Real bank acceptance, and whether this actually shortens account restrictions, are unmeasured. The key is app-held in the prototype. The policy parser handles five rule types and turns everything else into a question. Production would need separate privacy, security and regulatory review. If a probe failed in the frozen run, name it here by ID rather than waiting to be asked.
**Evidence:** `CONTROLS_SLIDE.md` README limits; frozen probe results ⏳.

---

## Numbers to have ready ⏳

Fill these from eval-verified records only (`python3 tools/results.py report --markdown`):

- Security probes blocked / attempted, plus controls allowed, on the live service.
- Bank-verifier reference run: SP-25 to SP-32 (recorded in `agents/execution/out/results.jsonl`, split `reference`). Say "reference run" if quoting it, never "live".
- Policy parser: correct, held and leaks out of 40, for the code parser and for Gemini.
- Injections: claim status unchanged, out of 25.
