/**
 * The phone app's Home and Account screens as plain data, built only from the
 * backend's JSON (alibi/app/UI.md): the passport context and each
 * transaction's detail context. Nothing here adds a bank, merchant or event
 * the backend did not serve.
 */

import type { ClaimStatus, LinkState, Money, PassportHome, Trust } from '@/lib/api/types';
// Relative, not `@/`, so the unit tests (tests/home.test.ts) can import this file directly.
import { day as dayOf, money } from '../../lib/format';

type Request = PassportHome['requests'][number];

/**
 * Whether someone may act as the holder: every visitor on a simulated site (they act as Maya in
 * the recorded demo), the signed demo session on the local live demo, nobody otherwise. The
 * export endpoints refuse anyone else.
 */
export function holderSignedIn(locals: App.Locals, simulated: boolean): boolean {
  if (simulated) return true;
  return Boolean(locals.demoHolder);
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
  /** The bank's id, for requests (trusted banks are marked by id). */
  audience?: string;
  title: string;
  at: string;
  /** `awaiting` etc. for requests; `open` or `resolved` for alerts. */
  state: RequestState | 'open' | 'resolved';
  label: string;
  href: string | null;
  /** The reply-by date, for requests. */
  due?: string;
}

/**
 * Bank requests and alerts, those waiting on her first. Among those, the soonest reply-by date
 * comes first; everything else is newest first.
 */
export function activity(data: PassportHome): ActivityItem[] {
  const requests: ActivityItem[] = data.requests.map((r) => {
    const state = requestState(r.status);
    const n = r.question_count;
    return {
      id: r.request_id,
      kind: 'request',
      from: r.recipient,
      audience: audienceOf(r),
      title: n === 1 ? 'Asks 1 question' : `Asks ${n} questions`,
      at: r.received_at,
      state,
      label: REQUEST_LABEL[state],
      href: requestHref(r.request_id),
      due: r.expires_at,
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
  const waiting = (i: ActivityItem) => i.state === 'awaiting' || i.state === 'open';
  const byDue = (a: ActivityItem, b: ActivityItem) => {
    if (!waiting(a)) return 0;
    if (a.due && b.due) return a.due.localeCompare(b.due);
    return a.due ? -1 : b.due ? 1 : 0;
  };
  return [...requests, ...alerts].sort(
    (a, b) => Number(!waiting(a)) - Number(!waiting(b)) || byDue(a, b) || b.at.localeCompare(a.at),
  );
}

/** The awaiting request whose reply is due first, or null when nothing waits on her. */
export function nextDue(data: PassportHome): { recipient: string; expires_at: string } | null {
  const waiting = data.requests
    .filter((r) => requestState(r.status) === 'awaiting' && r.expires_at)
    .sort((a, b) => a.expires_at.localeCompare(b.expires_at));
  const first = waiting[0];
  return first ? { recipient: first.recipient, expires_at: first.expires_at } : null;
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
  reconciliation: Reconciliation;
  claims: {
    claim_id: string;
    question: string;
    status: ClaimStatus;
    /** `active`, or `invalidated` once a source revision or draft change made it stale. */
    state?: string;
    request_id?: string;
  }[];
}

/** One grouped term of a reconciliation, as `calc_terms()` in alibi/app/common.py serves it. */
export interface ReconLine {
  label: string;
  kind: string;
  sign: '+' | '-';
  amount: Money;
  source_ids: string[];
}

/** `GET /txn/<id>`'s reconciliation (`_reconciliation()` in alibi/app/views.py). */
export interface Reconciliation {
  expression: string;
  matches: boolean;
  lines: ReconLine[];
  result?: Money;
  difference?: Money | null;
  compared_to?: { label: string; amount: Money };
}

export function isReconLine(value: unknown): value is ReconLine {
  const v = value as ReconLine;
  return (
    typeof v === 'object' &&
    v !== null &&
    typeof v.label === 'string' &&
    typeof v.kind === 'string' &&
    (v.sign === '+' || v.sign === '-') &&
    typeof v.amount?.minor === 'number' &&
    typeof v.amount?.currency === 'string' &&
    Array.isArray(v.source_ids)
  );
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
  calculation: { expression: string; matches: boolean; lines: ReconLine[] } | null;
  /** The linked record that explains the movement, when one does. */
  explainedBy: TxnDetail['sources'][number] | null;
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
      const lines = (d?.reconciliation.lines ?? []).filter(isReconLine);
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
          d && lines.length > 0
            ? {
                expression: `${d.reconciliation.expression} (pence)`,
                matches: d.reconciliation.matches,
                lines,
              }
            : null,
        explainedBy: explaining ?? null,
      };
    });
}

/** The component records a reconciliation adds up: sales, refunds and fees, not the payout itself. */
const COMPONENT_KINDS = new Set(['order', 'refund', 'fee', 'receipt']);

/**
 * One plain sentence about a transaction, built only from structured fields the backend sent
 * (link state, the explaining record and the reconciliation), never from free text.
 */
export function explain(entry: ContextEntry): string {
  if (entry.linkState === 'ambiguous')
    return 'More than one record could explain this. Alibi has not picked one.';
  if (entry.linkState === 'unmatched')
    return 'Only your bank has a record of this. No connected merchant has sent anything that explains it yet.';
  const src = entry.explainedBy;
  if (!src) return 'Linked evidence details are unavailable here.';
  const kind = src.kind.replaceAll('_', ' ');
  const issuer = plainName(src.issuer);
  const calc = entry.calculation;
  if (calc?.matches) {
    const n = calc.lines
      .filter((l) => COMPONENT_KINDS.has(l.kind))
      .reduce((sum, l) => sum + l.source_ids.length, 0);
    if (n > 0) {
      const reference = entry.references[0] ?? src.record_id;
      return `Matches ${issuer}'s ${kind} ${reference}, which settled on ${dayOf(src.occurred_at)}. The ${n} ${n === 1 ? 'record' : 'records'} it covers add up to this amount.`;
    }
  }
  return `Matches ${issuer}'s ${kind} of the same amount on ${dayOf(src.occurred_at)}.`;
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

// ------------------------------------------------------------ status pills --

/** A pill's colour; every pill also carries its words (never colour alone). */
export type Tone = 'review' | 'ok' | 'warn' | 'bad' | 'quiet';

export const STATUS_TONE: Record<string, Tone> = {
  // requests
  awaiting: 'review',
  approved: 'ok',
  sent: 'ok',
  declined: 'bad',
  expired: 'quiet',
  // alerts
  open: 'warn',
  resolved: 'quiet',
  // transactions
  linked: 'ok',
  ambiguous: 'warn',
  unmatched: 'quiet',
  // claims
  supported: 'ok',
  contradicted: 'bad',
  unresolved: 'warn',
  // merchant connections
  connected: 'ok',
  syncing: 'quiet',
  error: 'bad',
  disconnected: 'quiet',
};

export function toneOf(state: string): Tone {
  return STATUS_TONE[state] ?? 'quiet';
}

/** What a transaction's link state means to Maya. */
export const LINK_LABEL: Record<LinkState, string> = {
  linked: 'Explained',
  ambiguous: 'More than one possible match',
  unmatched: 'Not explained yet',
};

export const CONNECTION_LABEL: Record<PassportHome['connections'][number]['status'], string> = {
  connected: 'Connected',
  syncing: 'Syncing',
  error: 'Sync failed',
  disconnected: 'Disconnected',
};
