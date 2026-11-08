/**
 * What goes inside the passport folio (src/components/passport/PassportFolio.astro): the holder's
 * passport page, a file divider per merchant profile, a card per bank, and the pocket's summary.
 * `folioFromPassport` fills it from the holder's passport home, so the dashboard's passport shows
 * exactly the connections and records the backend holds. Only names, counts and dates are used.
 */
import type { PassportHome } from '@/lib/api/types';

export interface FolioProfile {
  name: string;
  /** A wordmark file in public/logos; without one the tab shows the name. */
  logo?: string;
  role: string;
  /** The divider's paper colour. */
  tint: string;
}

export interface FolioCard {
  bank: string;
  logo?: string;
  last4?: string;
  /** The card's colours: a known bank's, or plain ink. */
  kind: 'santander' | 'natwest' | 'plain';
}

export interface FolioContents {
  holder: string;
  subjectRef: string;
  /** The year the holder's history starts, if known. */
  since?: string;
  /** Back to front; the last one is the front divider. At most four fit. */
  profiles: FolioProfile[];
  /** What the front divider says about itself. */
  featured?: { role: string; title: string; stamp?: string; meta?: string };
  /** At most two fit. */
  cards: FolioCard[];
  /** Three short facts along the bottom of the passport page. */
  facts: { label: string; value: string }[];
  pocket: { title: string; sub: string };
}

/** Divider colours, back to front. */
export const FOLIO_TINTS = ['#e9e3f3', '#f6e3df', '#e3eaf6', '#fbf3e2'] as const;

const LOGOS: Record<string, string> = {
  amazon: '/logos/amazon.png',
  deliveroo: '/logos/deliveroo.png',
  uniqlo: '/logos/uniqlo.png',
  ucl: '/logos/ucl.png',
  natwest: '/logos/natwest.png',
  santander: '/logos/santander.png',
};

function logoFor(name: string): string | undefined {
  return LOGOS[name.trim().toLowerCase()];
}

function cardKind(name: string): FolioCard['kind'] {
  const n = name.trim().toLowerCase();
  return n === 'santander' || n === 'natwest' ? n : 'plain';
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString('en-GB')} ${n === 1 ? one : many}`;
}

function shortDay(iso: string): string {
  return new Date(iso).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  });
}

/** The holder's passport, filled from their passport home. */
export function folioFromPassport(data: PassportHome): FolioContents {
  const live = data.connections.filter((c) => c.status !== 'disconnected');
  const merchants = live.filter((c) => c.kind === 'merchant');
  const banks = live.filter((c) => c.kind === 'bank');
  const records = live.reduce((sum, c) => sum + c.record_count, 0);
  const years = data.transactions.map((t) => t.booked_at.slice(0, 4)).sort();
  const recipients = new Set(data.sharing_history.map((p) => p.recipient));

  const shown = merchants.slice(-4);
  const front = shown.at(-1);

  return {
    holder: data.holder.name,
    subjectRef: data.holder.subject_ref,
    since: years[0],
    profiles: shown.map((c, i) => ({
      name: c.name,
      logo: logoFor(c.name),
      role: plural(c.record_count, 'record'),
      tint: FOLIO_TINTS[(i + 4 - shown.length) % 4],
    })),
    featured: front && {
      role: front.trust === 'issuer_verified' ? 'Signed by the merchant' : 'Merchant records',
      title: front.name,
      stamp: front.trust === 'issuer_verified' ? 'signed' : undefined,
      meta: `${plural(front.record_count, 'record')} · synced ${shortDay(front.last_synced_at)}`,
    },
    cards: banks.slice(0, 2).map((c) => ({
      bank: c.name,
      logo: logoFor(c.name),
      kind: cardKind(c.name),
    })),
    facts: [
      { label: 'Merchants', value: `${merchants.length} connected` },
      { label: 'Banks', value: `${banks.length} read-only` },
      {
        label: 'Shared with',
        value: recipients.size === 0 ? 'No one yet' : plural(recipients.size, 'bank'),
      },
    ],
    pocket: {
      title: plural(records, 'record'),
      sub: `${plural(merchants.length, 'merchant')} · ${plural(banks.length, 'bank')}`,
    },
  };
}
