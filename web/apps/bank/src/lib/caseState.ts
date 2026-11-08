/**
 * Where each case stands with Alibi, from the bank service's own inbox: nothing asked yet, a
 * request waiting for the customer, or a package received. Without a live inbox (the built-in
 * preview) nothing has been sent, so every case starts at "not requested" and the full request
 * flow stays visible.
 */
import type { FraudCase } from '../data/cases';

import type { AnalystInbox } from './types';

export type AlibiState =
  | { kind: 'unavailable' }
  | { kind: 'closed' }
  | { kind: 'not_requested' }
  | { kind: 'requested'; requestId: string; sentAt: string }
  | { kind: 'received'; requestId: string; packageId: string; href: string; receivedAt: string };

export function alibiState(c: FraudCase, inbox: AnalystInbox | null): AlibiState {
  if (c.status === 'closed') return { kind: 'closed' };
  if (!c.request) return { kind: 'unavailable' };
  const id = c.request.id;
  const pkg = inbox?.packages.find((p) => p.request_id === id);
  if (pkg)
    return {
      kind: 'received',
      requestId: id,
      packageId: pkg.package_id,
      href: pkg.href,
      receivedAt: pkg.received_at,
    };
  const sent = inbox?.requests_sent.find((r) => r.request_id === id);
  if (sent) return { kind: 'requested', requestId: id, sentAt: sent.sent_at };
  return { kind: 'not_requested' };
}

/** The status column's words; never colour alone. */
export function statusLabel(c: FraudCase, s: AlibiState): string {
  if (s.kind === 'received') return 'Alibi received';
  if (s.kind === 'requested') return 'Awaiting customer';
  if (c.status === 'awaiting_customer') return 'Awaiting customer';
  if (c.status === 'closed') return 'Closed';
  return 'Open';
}
