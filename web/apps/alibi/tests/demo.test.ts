import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { breakdown, credit, formatGBP, netMinor, request } from '../src/data/demo';

// The team's canonical fixture, owned by `data`. Read-only.
const caseFile = new URL('../../../../agents/data/out/cases/case_0001.json', import.meta.url);
const case1 = JSON.parse(readFileSync(caseFile, 'utf8'));

describe('demo data', () => {
  it('reconciles to the credit', () => {
    expect(netMinor()).toBe(credit.amountMinor);
    expect(formatGBP(credit.amountMinor)).toBe('£2,160.00');
  });

  it('matches the canonical fixture, development case 1', () => {
    const row = case1.bank_deliveries.find(
      (d: { row: { txn_id: string } }) => d.row.txn_id === credit.txnId,
    ).row;
    expect(row.amount_minor).toBe(credit.amountMinor);
    expect(row.booked_at).toBe(credit.bookedAt);
    expect(row.payment_reference).toBe(credit.payoutReference);

    const stated = case1.holder_claim.stated_minor;
    expect([stated.sales, stated.refunds, stated.fees]).toEqual([
      breakdown.salesMinor,
      breakdown.refundsMinor,
      breakdown.feesMinor,
    ]);

    const rfc = case1.rfcs.find((r: { request_id: string }) => r.request_id === request.requestId);
    expect(rfc.issued_at).toBe(request.issuedAt);
  });
});
