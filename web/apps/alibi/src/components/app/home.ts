/**
 * The phone app's Home and Account screens as plain data, built only from the
 * backend's JSON (alibi/app/UI.md): the passport context and each
 * transaction's detail context. Nothing here adds a bank, merchant or event
 * the backend did not serve.
 */

import type { ClaimStatus, LinkState, Money, PassportHome, Trust } from '@/lib/api/types';
// Relative, not `@/`, so the unit tests (tests/home.test.ts) can import this file directly.
import { money } from '../../lib/format';

type Request = PassportHome['requests'][number];

/**
 * Whether the holder is signed in: the demo holder session on a local demo,
 * Clerk otherwise. The export endpoints refuse anyone else.
 */
export function holderSignedIn(locals: App.Locals): boolean {
  if (locals.demoHolder !== undefined) return Boolean(locals.demoHolder);
  try {
    return Boolean(locals.auth?.().userId);
  } catch {
    return false;
  }
}

// ------------------------------------------------------------- requests --

/**
 * Every disclosure needs Maya's own approval of the exact preview
 * (alibi/SPEC.md), so a request is only ever waiting on her, approved by
 * her, declined by her, sent or expired. A dispatch refusal sends it back to
 * her review.
 */
export type RequestState = 'awaiting' | 'approved' | 'declined' | 'sent' | 'expired';

export const REQUEST_LABEL: Record<RequestState, string> = {
  awaiting: 'Awaiting your review',
  approved: 'Approved by you',
  declined: 'Declined by you',
  sent: 'Sent',
  expired: 'Expired',
};

export function requestState(status: string): RequestState {
  switch (status) {
    case 'approved':
      return 'approved';
    case 'sent':
      return 'sent';
    case 'declined':
      return 'declined';
    case 'expired':
      return 'expired';
    default:
      return 'awaiting';
  }
}

/** The app's page for a request (the backend's `href` is a backend path). */
export function requestHref(requestId: string): string {
  return `/app/requests/${encodeURIComponent(requestId)}/`;
}

// ------------------------------------------------------------- activity --

export interface ActivityItem {
  id: string;
  kind: 'request' | 'alert';
  from: string;
  title: string;
  at: string;
  /** `awaiting` etc. for requests; `open` or `resolved` for alerts. */
  state: RequestState | 'open' | 'resolved';
  label: string;
  href: string | null;
}

/** Bank requests and alerts, those waiting on her first, then newest first. */
export function activity(data: PassportHome): ActivityItem[] {
  const requests: ActivityItem[] = data.requests.map((r) => {
    const state = requestState(r.status);
    const n = r.question_count;
    return {
      id: r.request_id,
      kind: 'request',
      from: r.recipient,
      title: n === 1 ? 'Asks 1 question' : `Asks ${n} questions`,
      at: r.received_at,
      state,
      label: REQUEST_LABEL[state],
      href: requestHref(r.request_id),
    };
  });
  const alerts: ActivityItem[] = data.alerts.map((a) => ({
    id: a.alert_id,
    kind: 'alert',
    from: 'Alibi alert',
    title: alertTitle(a.title),
    at: a.created_at,
    state: a.status,
    label: a.status === 'open' ? 'Open' : 'Resolved',
    href: null,
  }));
  const first = (i: ActivityItem) => (i.state === 'awaiting' || i.state === 'open' ? 0 : 1);
  return [...requests, ...alerts].sort((a, b) => first(a) - first(b) || b.at.localeCompare(a.at));
}

/**
 * Alert titles carry the amount in minor units ("Unexplained credit of
 * 216000 GBP"); show it as money ("£2,160.00") so it cannot read as £216,000.
 */
export function alertTitle(title: string): string {
  return title.replace(/\b(\d+) ([A-Z]{3})\b/g, (whole, minor: string, currency: string) => {
    try {
      return money({ minor: Number(minor), currency });
    } catch {
      return whole;
    }
  });
}

function names(list: string[]): string {
  return list.length === 1 ? list[0] : `${list.slice(0, -1).join(', ')} and ${list.at(-1)}`;
}

/** The one sentence at the top of Home: requests to review first, then approved ones to send. */
export function headline(data: PassportHome): string {
  const by = (state: RequestState) => [
    ...new Set(
      data.requests.filter((r) => requestState(r.status) === state).map((r) => r.recipient),
    ),
  ];
  const review = by('awaiting');
  const send = by('approved');
  if (review.length === 1) return `${review[0]} is waiting on your review.`;
  if (review.length > 1) return `${names(review)} are waiting on your review.`;
  if (send.length > 0) return `Your approved package for ${names(send)} is ready to send.`;
  return 'Nothing is waiting on you.';
}

// ---------------------------------------------------------------- banks --

export interface Bank {
  /** The backend's recipient id, e.g. `bank_a`. */
  audience: string;
  name: string;
  requests: number;
  awaiting: number;
  packagesSent: number;
  lastAt: string;
  /** The bank's account feed, when one is connected. */
  feed: PassportHome['connections'][number] | null;
}

/** The recipient id; an older backend without `audience` falls back to the display name. */
function audienceOf(r: Request): string {
  return r.audience || r.recipient;
}

/**
 * The bank's account feed. The backend names bank feeds `conn-<audience>-<subject>`
 * (`conn-bank-a-maya-001`); the match is anchored so `bank_a` never takes `conn-bank-ab-…`.
 */
function feedFor(data: PassportHome, audience: string) {
  const prefix = `conn-${audience.replace(/_/g, '-')}-`;
  return (
    data.connections.find((c) => c.kind === 'bank' && c.connection_id.startsWith(prefix)) ?? null
  );
}

/** The banks the backend knows: every recipient of a request or a package, and every bank feed. */
export function banks(data: PassportHome): Bank[] {
  const byAudience = new Map<string, Bank>();
  const bank = (audience: string, name: string) => {
    let b = byAudience.get(audience);
    if (!b) {
      b = {
        audience,
        name,
        requests: 0,
        awaiting: 0,
        packagesSent: 0,
        lastAt: '',
        feed: feedFor(data, audience),
      };
      byAudience.set(audience, b);
    }
    return b;
  };
  for (const r of data.requests) {
    const b = bank(audienceOf(r), r.recipient);
    b.requests += 1;
    if (requestState(r.status) === 'awaiting') b.awaiting += 1;
    if (r.received_at > b.lastAt) b.lastAt = r.received_at;
  }
  for (const s of data.sharing_history) {
    const req = data.requests.find((r) => r.request_id === s.request_id);
    const audience = s.audience || (req ? audienceOf(req) : s.recipient);
    const b = bank(audience, s.recipient);
    b.packagesSent += 1;
    if (s.sent_at > b.lastAt) b.lastAt = s.sent_at;
  }
  // A bank feed with no request yet is still one of her banks.
  for (const c of data.connections.filter((c) => c.kind === 'bank')) {
    if ([...byAudience.values()].some((b) => b.feed?.connection_id === c.connection_id)) continue;
    byAudience.set(c.connection_id, {
      audience: c.connection_id,
      name: plainName(c.name),
      requests: 0,
      awaiting: 0,
      packagesSent: 0,
      lastAt: c.last_synced_at,
      feed: c,
    });
  }
  return [...byAudience.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/** The merchants and other issuers her passport is connected to. */
export function merchants(data: PassportHome): PassportHome['connections'] {
  return data.connections.filter((c) => c.kind !== 'bank');
}

/** "Mock Marketplace (mock)" → "Mock Marketplace"; the backend marks its mocks. */
export function plainName(name: string): string {
  return name.replace(/\s*\(mock\)\s*$/i, '');
}

/** One or two letters for a monogram: "Bank A" → "A", "Mock Marketplace" → "MM". */
export function initials(name: string): string {
  const words = plainName(name)
    .replace(/[^\p{L}\p{N} ]/gu, ' ')
    .split(/\s+/)
    .filter(Boolean);
  if (words[0]?.toLowerCase() === 'bank' && words.length >= 2) return words[1].slice(0, 2);
  return words
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
    .toUpperCase();
}

// ---------------------------------------------------- financial context --

/** The parts of `GET /txn/<id>` (UI.md, passport/transaction.html) the timeline reads. */
export interface TxnDetail {
  txn: {
    txn_id: string;
    connection_name: string;
    trust: Trust;
    references: string[];
  };
  sources: {
    record_id: string;
    revision: number;
    kind: string;
    issuer: string;
    trust: Trust;
    occurred_at: string;
    amount: Money;
    status: string;
    summary: string;
    via_lookup: boolean;
    superseded: boolean;
  }[];
  reconciliation: { expression: string; matches: boolean; lines: unknown[] };
  claims: {
    claim_id: string;
    question: string;
    status: ClaimStatus;
    /** `active`, or `invalidated` once a source revision or draft change made it stale. */
    state?: string;
    request_id?: string;
  }[];
}

export function isTxnDetail(value: unknown): value is TxnDetail {
  const v = value as TxnDetail;
  return (
    typeof v === 'object' &&
    v !== null &&
    typeof v.txn?.txn_id === 'string' &&
    Array.isArray(v.sources) &&
    Array.isArray(v.claims) &&
    typeof v.reconciliation === 'object'
  );
}

export interface ContextEntry {
  id: string;
  at: string;
  title: string;
  amount: Money;
  direction: 'credit' | 'debit';
  linkState: LinkState;
  linkedCount: number;
  /** Where the bank row came from. */
  observedBy: string;
  references: string[];
  /** What the linked records say, in the issuer's words. */
  context: string;
  sources: TxnDetail['sources'];
  /** Current claims about this transaction, with the bank each answered. */
  claims: (TxnDetail['claims'][number] & { bank: string })[];
  /** The reconciliation, when the backend computed one against linked records. */
  calculation: { expression: string; matches: boolean } | null;
}

/**
 * A bank description as a title and a quieter line: "CARD (Synthetic Grocer)"
 * → title "Synthetic Grocer", line "Card". Words with digits (references)
 * keep their case; nothing is dropped.
 */
export function describe(description: string): { title: string; line: string } {
  const sentence = (s: string) => {
    const words = s.split(/\s+/).map((w) => (/\d/.test(w) ? w : w.toLowerCase()));
    const out = words.join(' ');
    return out.charAt(0).toUpperCase() + out.slice(1);
  };
  const m = /^(.*\S)\s*\(([^()]+)\)$/.exec(description.trim());
  if (m) return { title: m[2], line: sentence(m[1]) };
  return {
    title: description === description.toUpperCase() ? sentence(description) : description,
    line: '',
  };
}

/** Each transaction joined to its linked sources and claim states, newest first. */
export function contextTimeline(
  data: PassportHome,
  details: Map<string, TxnDetail>,
): ContextEntry[] {
  const bankFeed = data.connections.find((c) => c.kind === 'bank');
  return [...data.transactions]
    .sort((a, b) => b.booked_at.localeCompare(a.booked_at))
    .map((t) => {
      const d = details.get(t.txn_id);
      const sources = (d?.sources ?? []).filter((s) => !s.superseded);
      // The record that explains the movement: a settlement or payout of the same amount,
      // not whichever record the backend happens to list first.
      const amount = Math.abs(t.amount.minor);
      const explaining =
        sources.find((s) => s.kind === 'settlement' && s.amount.minor === amount) ??
        sources.find((s) => s.kind === 'payout' && s.amount.minor === amount) ??
        sources.find((s) => s.amount.minor === amount) ??
        (sources.length === 1 ? sources[0] : undefined);
      const claims = (d?.claims ?? [])
        .filter((c) => (c.state ?? 'active') === 'active')
        .map((c) => ({
          ...c,
          bank: data.requests.find((r) => r.request_id === c.request_id)?.recipient ?? '',
        }));
      return {
        id: t.txn_id,
        at: t.booked_at,
        title: t.description,
        amount: t.amount,
        direction: t.direction,
        linkState: t.link_state,
        linkedCount: t.linked_count,
        observedBy: plainName(d?.txn.connection_name ?? bankFeed?.name ?? 'Your bank'),
        references: d?.txn.references ?? [],
        context: explaining
          ? explaining.summary
          : sources.length > 0
            ? `${sources.length} related ${sources.length === 1 ? 'record' : 'records'} from your connections.`
            : 'No record from your connections explains this yet.',
        sources,
        claims,
        calculation:
          d && d.reconciliation.lines.length > 0
            ? {
                expression: `${d.reconciliation.expression} (pence)`,
                matches: d.reconciliation.matches,
              }
            : null,
      };
    });
}

/** Entries under a month heading, keeping their order. */
export function byMonth<T extends { at: string }>(entries: T[]): { month: string; entries: T[] }[] {
  const MONTHS = [
    'January',
    'February',
    'March',
    'April',
    'May',
    'June',
    'July',
    'August',
    'September',
    'October',
    'November',
    'December',
  ];
  const groups: { month: string; entries: T[] }[] = [];
  for (const e of entries) {
    const m = /^(\d{4})-(\d{2})/.exec(e.at);
    const month = m ? `${MONTHS[Number(m[2]) - 1]} ${m[1]}` : 'Earlier';
    const last = groups.at(-1);
    if (last?.month === month) last.entries.push(e);
    else groups.push({ month, entries: [e] });
  }
  return groups;
}

export const TRUST_MARK: Record<Trust, { mark: string; label: string }> = {
  issuer_verified: { mark: '●', label: 'Issuer verified' },
  bank_observed: { mark: '◆', label: 'Bank observed' },
  self_supplied: { mark: '○', label: 'Self-supplied' },
};

export const CLAIM_MARK: Record<ClaimStatus, { mark: string; label: string }> = {
  supported: { mark: '✓', label: 'Supported' },
  contradicted: { mark: '✕', label: 'Contradicted' },
  unresolved: { mark: '?', label: 'Unresolved' },
};

export const LINK_MARK: Record<LinkState, { mark: string; label: string }> = {
  linked: { mark: '=', label: 'Linked' },
  ambiguous: { mark: '~', label: 'Ambiguous' },
  unmatched: { mark: '–', label: 'Unmatched' },
};
