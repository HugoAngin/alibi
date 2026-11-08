# Encrypt the standalone bank snapshot

Candidate 5 persisted 30 readable attachments across two receipt-verified
public Case 1 packages. The finding is separate from the encrypted passport
store. See `s5-3c50476-ingest-followup-supplement.json`.

`BANK_STATE_ENCRYPTION.patch` is for the backend owner. Apply it after
`BANK_RFC_HARDENING.patch` and `BANK_RFC_UI_RETRY.patch`. The tested
composition also included the Worker delivery feedback and stage runner
patches. The bank keeps its existing JSON routes, receipt verification and
clock. It now persists an authenticated encrypted snapshot and refuses
plaintext or a wrong key at normal startup.

Before a hosted rollout, configure a stable, distinct `ALIBI_BANK_STATE_KEY`
in the backend host's secret store. Keep it separate from the state path.
The local fallback writes a mode-0600 key beside the snapshot. To retain
existing plaintext state, stop the bank and explicitly migrate its trusted
snapshot using the new runbook command with the intended key already set.
Legacy backups and the optional in-process bank sidecar are outside this fix.

Independent verification on disposable main `181d73e` with the composed
patches delivered two signed, independently verified receipts. The raw
bank snapshot contained only the format and ciphertext, with no adjacent
key file under configured-key operation. Reopening it recovered the exact
two verified packages and 30 attachments. Two fresh stage runner cycles
reached D1 through D3, shut down cleanly and released all four ports.
