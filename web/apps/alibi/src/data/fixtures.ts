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

/** Request detail for Bank A's request. Other requests have no fixture. */
export function requestFixture(requestId: string): RequestDetail | null {
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
