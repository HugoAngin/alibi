import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import {
  CREDIT,
  PAYOUT,
  SUBJECT,
  bankRows,
  merchantRecords,
  requests,
  sum,
} from '../src/data/case1';
import { passportFixture, requestFixture } from '../src/data/fixtures';

// The team's canonical fixture, owned by `data`. Read-only.
const caseFile = new URL('../../../../agents/data/out/cases/case_0001.json', import.meta.url);
const case1 = JSON.parse(readFileSync(caseFile, 'utf8'));

type Event = {
  record_id: string;
  kind: string;
  amount_minor: number;
  occurred_at: string;
  settled_at?: string;
};
const events: Event[] = [
  ...case1.merchant_deliveries.map((d: { event: Event }) => d.event),
  ...case1.merchant_lookup.map((d: { event: Event }) => d.event),
];

describe('case1 data matches agents/data case 1', () => {
  it('has the same subject, bank rows and merchant records', () => {
    expect(case1.rfcs[0].subject_ref).toBe(SUBJECT);
    for (const row of bankRows) {
      const actual = case1.bank_deliveries.find(
        (d: { row: { txn_id: string } }) => d.row.txn_id === row.txnId,
      ).row;
      expect([
        actual.booked_at,
        actual.amount_minor,
        actual.description,
        actual.counterparty_name,
      ]).toEqual([row.bookedAt, row.minor, row.description, row.counterparty]);
    }
    for (const rec of merchantRecords) {
      const actual = events.find((e) => e.record_id === rec.id)!;
      expect([actual.kind, actual.amount_minor]).toEqual([rec.kind, rec.minor]);
      expect(rec.occurredAt).toBe(rec.kind === 'payout' ? actual.settled_at : actual.occurred_at);
    }
  });

  it('has the same requests and question wording', () => {
    for (const r of [requests.bankA, requests.bankB]) {
      const actual = case1.rfcs.find((x: { request_id: string }) => x.request_id === r.requestId);
      expect([actual.issued_at, actual.expires_at]).toEqual([r.issuedAt, r.expiresAt]);
      expect(actual.questions).toEqual(r.questions);
    }
  });

  it('reconciles: £2,400 − £180 − £60 = £2,160', () => {
    expect([sum('sale'), sum('refund'), sum('fee')]).toEqual([240000, 18000, 6000]);
    expect(sum('sale') - sum('refund') - sum('fee')).toBe(CREDIT.minor);
  });
});

describe('request fixture follows the answer key', () => {
  const detail = requestFixture(requests.bankA.requestId)!;
  const answers = case1.gold.answers[requests.bankA.requestId];
  const refOf = (id: string) => (id.startsWith('txn-') ? `bank:${id}` : `merchant:${id}`);

  it('cites exactly the gold evidence set for each question', () => {
    for (const step of detail.trace) {
      const cited = step.sources.map((s) => refOf(s.record_id)).sort();
      expect(cited).toEqual([...answers[step.question_id].evidence_sets[0]].sort());
      expect(step.status).toBe(answers[step.question_id].status);
    }
  });

  it('never includes a forbidden record', () => {
    const included = detail.review.included.map((i) => refOf(i.record_id));
    for (const forbidden of case1.gold.forbidden_refs) expect(included).not.toContain(forbidden);
  });

  it('looks up the payout the release message names', () => {
    expect(case1.gold.requires_retrieval).toContain(PAYOUT.recordId);
    expect(detail.trace[0].lookups[0].args_summary).toContain(PAYOUT.reference);
  });

  it('has no fixture for unknown requests', () => {
    expect(requestFixture('rfc-nope')).toBeNull();
  });
});

describe('passport fixture', () => {
  it('shows every bank row and both requests', () => {
    const p = passportFixture();
    expect(p.transactions.map((t) => t.txn_id).sort()).toEqual(bankRows.map((r) => r.txnId).sort());
    expect(p.requests.map((r) => r.request_id)).toEqual([
      requests.bankA.requestId,
      requests.bankB.requestId,
    ]);
  });
});
