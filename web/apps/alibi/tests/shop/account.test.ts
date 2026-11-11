import { describe, expect, it } from 'vitest';

import view from '../../../../../agents/data/out/merchant_view/case_0001.json';
import {
  notifications,
  orders,
  payouts,
  purchases,
  refunds,
  totals,
} from '../../src/shop/data/account';

// Data's marketplace view of case 1 (agents/data/out/merchant_view/case_0001.json). Read-only.

describe("Maya's seller account matches data's marketplace view of case 1", () => {
  it('copies every order, refund, payout, notification and purchase exactly', () => {
    expect(
      orders.map((o) => [o.orderRef, o.title, o.soldAt, o.priceMinor, o.feeMinor, o.recordId]),
    ).toEqual(
      view.orders.map((o) => [
        o.order_ref,
        o.title,
        o.sold_at,
        o.price_minor,
        o.fee_minor,
        o.record_id,
      ]),
    );
    expect(
      refunds.map((r) => [r.refundRef, r.orderRef, r.refundedAt, r.amountMinor, r.recordId]),
    ).toEqual(
      view.refunds.map((r) => [
        r.refund_ref,
        r.order_ref,
        r.refunded_at,
        r.amount_minor,
        r.record_id,
      ]),
    );
    expect(payouts.map((p) => [p.payoutRef, p.amountMinor, p.settledAt, p.recordId])).toEqual(
      view.payouts.map((p) => [p.payout_ref, p.amount_minor, p.settled_at, p.record_id]),
    );
    expect(notifications.map((n) => n.body)).toEqual(view.notifications.map((n) => n.body));
    expect(purchases.map((p) => p.recordId)).toEqual(view.purchases.map((p) => p.record_id));
  });

  it('adds up to the £2,160 payout', () => {
    expect(totals.netMinor).toBe(view.totals.net_minor);
    expect(totals.salesMinor - totals.refundsMinor - totals.feesMinor).toBe(216000);
    expect(orders.reduce((s, o) => s + o.priceMinor, 0)).toBe(totals.salesMinor);
    expect(orders.reduce((s, o) => s + o.feeMinor, 0)).toBe(totals.feesMinor);
    expect(refunds.reduce((s, r) => s + r.amountMinor, 0)).toBe(totals.refundsMinor);
  });
});
