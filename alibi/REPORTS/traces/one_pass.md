# One-pass baseline dev run

Fixed retrieval: look up any payout reference named in a balance_release message. Gemini calls used: 4 of 4.

## contradiction (live)

### rfc_a_001 (run_35f55681be3a0b3e9e9e84c2)

- final statuses: {"q1": "unresolved"}
- model proposed: {"q1": "unresolved"}
- gaps: {"q1": "settlement_not_found: no settlement record for payout PO-5001 was retrieved; a payout record does not show a bank transfer. balance_release_not_settlement: a 'r"}
- lookups=0 observed_inputs=9 unsafe_proposals=0 model_calls=1 tokens=6639

```
  0 | gateway | tool_call | get_txn | ok | - | txn:txn_9001@1
  1 | gateway | tool_call | list_records | ok | - | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:rfd_2001@1, record:ord_1004@1, record:fee_3001@1, record:msg_7001@1, record:po_5001@1
  2 | one_pass | note | - | ok | Fixed retrieval strategy: look up any payout reference named in a balance_release message. | -
  3 | one_pass | note | lookup_settlement | ok | Fixed retrieval: none of 1 balance_release message(s) names a payout reference; no settlement lookup performed. | record:msg_7001@1
  4 | one_pass | note | generate | ok | The model returned a proposal. | -
  5 | one_pass | note | - | ok | Pre-policy proposal recorded. | -
  6 | one_pass | note | - | ok | Question classified as source_of_funds by keyword. | -
  7 | one_pass | validation | settlement_candidates | ok | link unmatched: 0 settled candidate(s), 1 provisional; provisional po_5001@1 (216000 GBP, settlement not retrieved, credit carries no reference) | txn:txn_9001@1, record:po_5001@1
  8 | one_pass | status_change | - | ok | No settlement record for the candidate payout. | txn:txn_9001@1, record:po_5001@1
  9 | one_pass | validation | build_body | ok | Policy gate applied to the claims. | record:po_5001@1, txn:txn_9001@1
 10 | one_pass | note | - | ok | One-pass baseline counts. | -
```

### rfc_b_001 (run_e7efb3bfa9029929685b2959)

- final statuses: {"q1": "supported", "q2": "supported"}
- model proposed: {"q1": "supported", "q2": "supported"}
- gaps: {"q1": "", "q2": ""}
- lookups=0 observed_inputs=8 unsafe_proposals=0 model_calls=1 tokens=7103

```
  0 | gateway | tool_call | list_records | ok | - | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:rfd_2001@1, record:ord_1004@1, record:fee_3001@1, record:msg_7001@1, record:po_5001@1
  1 | one_pass | note | - | ok | Fixed retrieval strategy: look up any payout reference named in a balance_release message. | -
  2 | one_pass | note | lookup_settlement | ok | Fixed retrieval: none of 1 balance_release message(s) names a payout reference; no settlement lookup performed. | record:msg_7001@1
  3 | one_pass | note | generate | ok | The model returned a proposal. | -
  4 | one_pass | note | - | ok | Pre-policy proposal recorded. | -
  5 | one_pass | note | - | ok | Question classified as sales_total by keyword. | -
  6 | one_pass | calculation | - | ok | sales_total: 240000 pence GBP | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:ord_1004@1
  7 | one_pass | status_change | - | ok | 4 documented records sum to 240000 pence GBP. | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:ord_1004@1
  8 | one_pass | note | - | ok | Question classified as refunds_total by keyword. | -
  9 | one_pass | calculation | - | ok | refunds_total: 18000 pence GBP | record:rfd_2001@1
 10 | one_pass | status_change | - | ok | 1 documented records sum to 18000 pence GBP. | record:rfd_2001@1, record:ord_1003@1
 11 | one_pass | validation | build_body | ok | Policy gate applied to the claims. | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:ord_1004@1, record:rfd_2001@1
 12 | one_pass | note | - | ok | One-pass baseline counts. | -
```

## supported (live)

### rfc_a_001 (run_35f55681be3a0b3e9e9e84c2)

- final statuses: {"q1": "unresolved"}
- model proposed: {"q1": "unresolved"}
- gaps: {"q1": "settlement_not_found: no settlement record for payout PO-5001 was retrieved; a payout record does not show a bank transfer. balance_release_not_settlement: a 'r"}
- lookups=0 observed_inputs=9 unsafe_proposals=0 model_calls=1 tokens=6627

```
  0 | gateway | tool_call | get_txn | ok | - | txn:txn_9001@1
  1 | gateway | tool_call | list_records | ok | - | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:rfd_2001@1, record:ord_1004@1, record:fee_3001@1, record:msg_7001@1, record:po_5001@1
  2 | one_pass | note | - | ok | Fixed retrieval strategy: look up any payout reference named in a balance_release message. | -
  3 | one_pass | note | lookup_settlement | ok | Fixed retrieval: none of 1 balance_release message(s) names a payout reference; no settlement lookup performed. | record:msg_7001@1
  4 | one_pass | note | generate | ok | The model returned a proposal. | -
  5 | one_pass | note | - | ok | Pre-policy proposal recorded. | -
  6 | one_pass | note | - | ok | Question classified as source_of_funds by keyword. | -
  7 | one_pass | validation | settlement_candidates | ok | link unmatched: 0 settled candidate(s), 1 provisional; provisional po_5001@1 (216000 GBP, settlement not retrieved, reference matches the credit) | txn:txn_9001@1, record:po_5001@1
  8 | one_pass | status_change | - | ok | No settlement record for the candidate payout. | txn:txn_9001@1, record:po_5001@1
  9 | one_pass | validation | build_body | ok | Policy gate applied to the claims. | record:po_5001@1, txn:txn_9001@1
 10 | one_pass | note | - | ok | One-pass baseline counts. | -
```

### rfc_b_001 (run_e7efb3bfa9029929685b2959)

- final statuses: {"q1": "supported", "q2": "supported"}
- model proposed: {"q1": "supported", "q2": "supported"}
- gaps: {"q1": "", "q2": ""}
- lookups=0 observed_inputs=8 unsafe_proposals=0 model_calls=1 tokens=7082

```
  0 | gateway | tool_call | list_records | ok | - | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:rfd_2001@1, record:ord_1004@1, record:fee_3001@1, record:msg_7001@1, record:po_5001@1
  1 | one_pass | note | - | ok | Fixed retrieval strategy: look up any payout reference named in a balance_release message. | -
  2 | one_pass | note | lookup_settlement | ok | Fixed retrieval: none of 1 balance_release message(s) names a payout reference; no settlement lookup performed. | record:msg_7001@1
  3 | one_pass | note | generate | ok | The model returned a proposal. | -
  4 | one_pass | note | - | ok | Pre-policy proposal recorded. | -
  5 | one_pass | note | - | ok | Question classified as sales_total by keyword. | -
  6 | one_pass | calculation | - | ok | sales_total: 240000 pence GBP | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:ord_1004@1
  7 | one_pass | status_change | - | ok | 4 documented records sum to 240000 pence GBP. | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:ord_1004@1
  8 | one_pass | note | - | ok | Question classified as refunds_total by keyword. | -
  9 | one_pass | calculation | - | ok | refunds_total: 18000 pence GBP | record:rfd_2001@1
 10 | one_pass | status_change | - | ok | 1 documented records sum to 18000 pence GBP. | record:rfd_2001@1
 11 | one_pass | validation | build_body | ok | Policy gate applied to the claims. | record:ord_1001@1, record:ord_1002@1, record:ord_1003@1, record:ord_1004@1, record:rfd_2001@1
 12 | one_pass | note | - | ok | One-pass baseline counts. | -
```

