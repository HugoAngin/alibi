/**
 * Screen contexts built from case 1, in the backend's JSON shapes
 * (src/lib/api/types.ts). Used when the backend is not connected, so every
 * screen still tells the canonical story. The trace and review follow the
 * case's answer key; they are not the output of a live run, and the screens
 * say so whenever they show fixture data. Nothing here invents a result: call
 * counts, digests and keys that only a live run produces are left empty.
 */

import type { PassportHome, RequestDetail } from '@/lib/api/types';
import {
  CREDIT,
  MERCHANT,
  PAYOUT,
  RELEASE,
  SUBJECT,
  bankRows,
  covered,
  merchantRecords,
  requests,
  sum,
} from './case1';

const GBP = (minor: number) => ({ minor, currency: 'GBP' });
const LABEL: Record<string, string> = { sale: 'Sale', fee: 'Platform fee', refund: 'Refund' };

export function passportFixture(): PassportHome {
  return {
    holder: { name: 'Maya', subject_ref: SUBJECT },
    connections: [
      {
        connection_id: 'conn-merchant',
        name: MERCHANT.name,
        kind: 'merchant',
        status: 'connected',
        trust: 'issuer_verified',
        last_synced_at: '2026-10-26T08:05:00Z',
        record_count: merchantRecords.length - 1,
        href: '#',
      },
      {
        connection_id: 'conn-bank',
        name: 'Current account',
        kind: 'bank',
        status: 'connected',
        trust: 'bank_observed',
        last_synced_at: '2026-10-28T07:30:00Z',
        record_count: bankRows.length,
        href: '#',
      },
    ],
    transactions: [...bankRows].reverse().map((r) => ({
      txn_id: r.txnId,
      revision: 1,
      booked_at: r.bookedAt,
      description:
        r.txnId === CREDIT.txnId
          ? `${MERCHANT.name} payout ${PAYOUT.reference}`
          : `${r.description} · ${r.counterparty}`,
      amount: GBP(r.minor),
      direction: r.minor >= 0 ? 'credit' : 'debit',
      link_state: r.txnId === CREDIT.txnId ? 'linked' : 'unmatched',
      linked_count: r.txnId === CREDIT.txnId ? 1 : 0,
      href: '#',
    })),
    evidence_status: {
      linked: 1,
      ambiguous: 0,
      unmatched: bankRows.length - 1,
      total: bankRows.length,
      last_checked_at: '2026-11-02T09:03:00Z',
      superseded_sources: 0,
    },
    alerts: [
      {
        alert_id: 'alert-0001-01',
        kind: 'settlement_discrepancy',
        severity: 'warning',
        title: `Your ${'£2,160'} credit has no settlement proof yet: the marketplace only reported a release to your seller balance`,
        created_at: '2026-10-28T07:30:00Z',
        status: 'resolved',
        href: '#',
      },
    ],
    rules: [
      {
        rule_id: 'rule-01',
        text: 'Never share what I buy.',
        status: 'active',
        summary: 'Deny your own purchases on the marketplace, for every recipient.',
        href: '#',
      },
      {
        rule_id: 'rule-default',
        text: 'Share only what a question needs.',
        status: 'active',
        summary: 'Default minimum disclosure. Always on.',
        href: '#',
      },
    ],
    requests: [
      {
        request_id: requests.bankA.requestId,
        recipient: 'Bank A',
        question_count: requests.bankA.questions.length,
        received_at: requests.bankA.issuedAt,
        expires_at: requests.bankA.expiresAt,
        status: 'draft',
        href: `/app/requests/${requests.bankA.requestId}/`,
      },
      {
        request_id: requests.bankB.requestId,
        recipient: 'Bank B',
        question_count: requests.bankB.questions.length,
        received_at: requests.bankB.issuedAt,
        expires_at: requests.bankB.expiresAt,
        status: 'new',
        href: `/app/requests/${requests.bankB.requestId}/`,
      },
    ],
    sharing_history: [],
  };
}

/** Request detail for Bank A's or Bank B's request. Other requests have no fixture. */
export function requestFixture(requestId: string): RequestDetail | null {
  if (requestId === requests.bankB.requestId) return bankBFixture();
  if (requestId !== requests.bankA.requestId) return null;
  const bank = { record_id: CREDIT.txnId, revision: 1, trust: 'bank_observed' as const, href: '#' };
  const payout = {
    record_id: PAYOUT.recordId,
    revision: 1,
    trust: 'issuer_verified' as const,
    href: '#',
  };
  const coveredRefs = covered.map((r) => ({
    record_id: r.id,
    revision: 1,
    trust: 'issuer_verified' as const,
    href: '#',
  }));
  const [sales, refunds, fees] = [sum('sale'), sum('refund'), sum('fee')];
  const net = sales - refunds - fees;
  const at = '2026-11-02T09:03:00Z';

  return {
    request: {
      request_id: requests.bankA.requestId,
      parent_request_id: null,
      issuer: 'Bank A',
      audience: 'bank_a',
      subject_ref: SUBJECT,
      received_at: requests.bankA.issuedAt,
      expires_at: requests.bankA.expiresAt,
      period: null,
      txn_ids: [CREDIT.txnId],
      questions: [...requests.bankA.questions],
      status: 'draft',
      signature: { verified: true, key_id: '' },
      href: '#',
    },
    investigation: {
      status: 'complete',
      can_run: false,
      run_action: `/requests/${requests.bankA.requestId}/investigate`,
      started_at: null,
      completed_at: null,
      budget: { calls_used: 0, calls_cap: 0 },
      roles: [],
      failure_reason: null,
    },
    trace: [
      {
        step_id: 'step-sof',
        question_id: 'q.source_of_funds',
        question: requests.bankA.questions[0].text,
        claim: {
          claim_id: 'claim-sof',
          text: `The credit is ${MERCHANT.name} payout ${PAYOUT.reference}, settled to the bank before the credit was booked.`,
          status: 'supported',
        },
        lookups: [
          {
            tool: 'merchant.lookup',
            args_summary: `payout_reference=${PAYOUT.reference}`,
            result_summary: `${PAYOUT.recordId} r1: payout settled to the bank on 27 Oct 2026, 16:00 UTC, £2,160.00. The pushed record ${RELEASE.recordId} only released the money to the seller balance.`,
            at,
          },
        ],
        sources: [bank, payout],
        calculation: null,
        status: 'supported',
        status_reason: `Settled 27 Oct 16:00 UTC, before the credit was booked 28 Oct 06:30 UTC; reference ${PAYOUT.reference} matches the bank row.`,
        at,
      },
      {
        step_id: 'step-breakdown',
        question_id: 'q.breakdown',
        question: requests.bankA.questions[1].text,
        claim: {
          claim_id: 'claim-breakdown',
          text: '£2,400.00 sales − £180.00 refunds − £60.00 fees = £2,160.00.',
          status: 'supported',
        },
        lookups: [],
        sources: [bank, payout, ...coveredRefs],
        calculation: {
          expression: `${sales} − ${refunds} − ${fees} = ${net}`,
          result: GBP(net),
          computed_by: 'code',
          matches: net === CREDIT.minor,
        },
        status: 'supported',
        status_reason: 'The 18 records the payout covers add up to the credit.',
        at,
      },
    ],
    review: {
      claims: [
        {
          claim_id: 'claim-sof',
          question_id: 'q.source_of_funds',
          question: requests.bankA.questions[0].text,
          status: 'supported',
          rationale: `${MERCHANT.name} payout ${PAYOUT.reference} settled to your bank on 27 Oct, the day before the credit.`,
          href: '#step-sof',
        },
        {
          claim_id: 'claim-breakdown',
          question_id: 'q.breakdown',
          question: requests.bankA.questions[1].text,
          status: 'supported',
          rationale:
            '8 sales (£2,400.00) less 2 refunds (£180.00) and 8 fees (£60.00) make £2,160.00.',
          href: '#step-breakdown',
        },
      ],
      included: [
        {
          item_id: `inc-${CREDIT.txnId}`,
          question_id: 'q.source_of_funds',
          label: 'The £2,160.00 credit',
          kind: 'bank transaction',
          record_id: CREDIT.txnId,
          revision: 1,
          trust: 'bank_observed',
          reason: 'The payment the bank asked about.',
          removable: false,
          remove_action: null,
        },
        {
          item_id: `inc-${PAYOUT.recordId}`,
          question_id: 'q.source_of_funds',
          label: `Payout ${PAYOUT.reference}`,
          kind: 'payout',
          record_id: PAYOUT.recordId,
          revision: 1,
          trust: 'issuer_verified',
          reason: 'Shows the money reached your bank before the credit.',
          removable: false,
          remove_action: null,
        },
        ...covered.map((r) => ({
          item_id: `inc-${r.id}`,
          question_id: 'q.breakdown',
          label: `${LABEL[r.kind]} £${(r.minor / 100).toFixed(2)}`,
          kind: r.kind,
          record_id: r.id,
          revision: 1,
          trust: 'issuer_verified' as const,
          reason: 'Part of the £2,160.00 the payout covers.',
          removable: false,
          remove_action: null,
        })),
      ],
      excluded: [
        {
          item_id: 'exc-txn-0001-001',
          label: 'Rent payment, 6 Oct',
          reason: 'Not needed for either question.',
          rule_id: null,
        },
        {
          item_id: 'exc-txn-0001-002',
          label: 'Card payment, 8 Oct',
          reason: 'Not needed for either question.',
          rule_id: null,
        },
        {
          item_id: 'exc-txn-0001-003',
          label: 'Card payment, 10 Oct',
          reason: 'Not needed for either question.',
          rule_id: null,
        },
        {
          item_id: 'exc-rec-0001-019',
          label: 'Your purchase on the marketplace, 14 Oct',
          reason: 'Your rule: never share what I buy.',
          rule_id: 'rule-01',
        },
      ],
      generalised: [],
      gaps: [],
      counts: { included: 2 + covered.length, excluded: 4, generalised: 0, gaps: 0 },
      approval: {
        status: 'pending',
        recipient: 'Bank A',
        audience: 'bank_a',
        action: `/requests/${requests.bankA.requestId}/approve`,
        digest: '',
        policy_version: '',
        valid_from: '',
        valid_until: '',
        approved_at: null,
        invalidated_reason: null,
      },
      preview_href: '#',
    },
  };
}

/**
 * Bank B's different question, answered from the same stored records: no new
 * import, its own selection, its own approval (SPEC: reuse challenge).
 */
function bankBFixture(): RequestDetail {
  const rq = requests.bankB;
  const asked = covered.filter((r) => r.kind === 'sale' || r.kind === 'refund');
  const sales = asked.filter((r) => r.kind === 'sale');
  const refunds = asked.filter((r) => r.kind === 'refund');
  const at = '2026-11-04T14:02:00Z';
  const src = asked.map((r) => ({
    record_id: r.id,
    revision: 1,
    trust: 'issuer_verified' as const,
    href: '#',
  }));
  const gbp = (minor: number) => `£${(minor / 100).toFixed(2)}`;

  return {
    request: {
      request_id: rq.requestId,
      parent_request_id: null,
      issuer: 'Bank B',
      audience: 'bank_b',
      subject_ref: SUBJECT,
      received_at: rq.issuedAt,
      expires_at: rq.expiresAt,
      period: { from: '2026-10-01T00:00:00Z', to: '2026-11-01T00:00:00Z' },
      txn_ids: [],
      questions: [...rq.questions],
      status: 'draft',
      signature: { verified: true, key_id: '' },
      href: '#',
    },
    investigation: {
      status: 'complete',
      can_run: false,
      run_action: `/requests/${rq.requestId}/investigate`,
      started_at: null,
      completed_at: null,
      budget: { calls_used: 0, calls_cap: 0 },
      roles: [],
      failure_reason: null,
    },
    trace: [
      {
        step_id: 'step-period',
        question_id: 'q.sales_refunds_in_period',
        question: rq.questions[0].text,
        claim: {
          claim_id: 'claim-period',
          text: `${sales.length} sales of ${gbp(sum('sale'))} and ${refunds.length} refunds of ${gbp(sum('refund'))} in October 2026.`,
          status: 'supported',
        },
        lookups: [],
        sources: src,
        calculation: {
          expression: `${sales.map((r) => r.minor).join(' + ')} = ${sum('sale')}`,
          result: GBP(sum('sale')),
          computed_by: 'code',
          matches: true,
        },
        status: 'supported',
        status_reason: `Sales and refunds dated 1–31 Oct, from records already in your passport. Refunds: ${refunds.map((r) => r.minor).join(' + ')} = ${sum('refund')}.`,
        at,
      },
    ],
    review: {
      claims: [
        {
          claim_id: 'claim-period',
          question_id: 'q.sales_refunds_in_period',
          question: rq.questions[0].text,
          status: 'supported',
          rationale: `${sales.length} sales (${gbp(sum('sale'))}) and ${refunds.length} refunds (${gbp(sum('refund'))}) dated in October.`,
          href: '#step-period',
        },
      ],
      included: asked.map((r) => ({
        item_id: `inc-b-${r.id}`,
        question_id: 'q.sales_refunds_in_period',
        label: `${LABEL[r.kind]} ${gbp(r.minor)}`,
        kind: r.kind,
        record_id: r.id,
        revision: 1,
        trust: 'issuer_verified' as const,
        reason: 'A sale or refund in the period Bank B asked about.',
        removable: false,
        remove_action: null,
      })),
      excluded: [
        {
          item_id: 'exc-b-fees',
          label: 'Platform fees (8 records)',
          reason: 'Bank B asked about sales and refunds only.',
          rule_id: null,
        },
        {
          item_id: 'exc-b-credit',
          label: 'The £2,160.00 credit and its payout',
          reason: 'Not part of this question.',
          rule_id: null,
        },
        {
          item_id: 'exc-b-bank',
          label: 'Rent and card payments',
          reason: 'Not needed for this question.',
          rule_id: null,
        },
        {
          item_id: 'exc-b-purchase',
          label: 'Your purchase on the marketplace, 14 Oct',
          reason: 'Your rule: never share what I buy.',
          rule_id: 'rule-01',
        },
      ],
      generalised: [],
      gaps: [],
      counts: { included: asked.length, excluded: 4, generalised: 0, gaps: 0 },
      approval: {
        status: 'pending',
        recipient: 'Bank B',
        audience: 'bank_b',
        action: `/requests/${rq.requestId}/approve`,
        digest: '',
        policy_version: '',
        valid_from: '',
        valid_until: '',
        approved_at: null,
        invalidated_reason: null,
      },
      preview_href: '#',
    },
  };
}
