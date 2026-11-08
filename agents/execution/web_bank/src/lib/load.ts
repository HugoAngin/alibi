/**
 * Load the console's screens from the live bank service, converting its JSON (src/lib/bankApi.ts),
 * or fall back to the built-in case 1 preview and say why. A real 404 from the bank stays "not
 * found" (no fallback), so another bank's package ID never shows the preview instead.
 */

import { loadContext, type Loaded } from '@alibi/ui/api';

import { inboxFixture, packageFixture } from '@/data/analyst';

import {
  BANK_ROUTES,
  isBankInbox,
  isBankPackage,
  toAnalystInbox,
  toAnalystPackage,
  type BankInbox,
  type BankPackage,
  type BankTarget,
  type Who,
} from './bankApi';
import type { AnalystInbox, AnalystPackage } from './types';

const NOT_FOUND = 'The backend has no such item.';

export async function loadPackage(
  id: string,
  target: BankTarget,
  who: Who,
  headers: Record<string, string>,
): Promise<Loaded<AnalystPackage>> {
  const raw = await loadContext<BankPackage>({
    origin: target.origin,
    path: BANK_ROUTES.package(target, id),
    isShape: isBankPackage,
    fixture: () => null,
    headers,
  });
  if (raw.source === 'live') return { source: 'live', data: toAnalystPackage(raw.data, who, target) };
  if (raw.source === 'missing' && raw.reason === NOT_FOUND) return raw;
  const fallback = packageFixture(id);
  const reason = raw.source === 'missing' ? raw.reason : 'The bank service is not connected.';
  return fallback ? { source: 'fixture', data: fallback, reason } : { source: 'missing', reason };
}

export async function loadInbox(
  target: BankTarget,
  who: Who,
  headers: Record<string, string>,
): Promise<Loaded<AnalystInbox>> {
  const raw = await loadContext<BankInbox>({
    origin: target.origin,
    path: BANK_ROUTES.inbox(target),
    isShape: isBankInbox,
    fixture: () => null,
    headers,
  });
  if (raw.source === 'live') return { source: 'live', data: toAnalystInbox(raw.data, who) };
  const reason = raw.source === 'missing' ? raw.reason : 'The bank service is not connected.';
  return { source: 'fixture', data: inboxFixture(), reason };
}
