/**
 * What the case queue pages (/, /cases/<id>/, /cases/<id>/request/) need from the server: the
 * analyst's session, each case's Alibi state from the bank service's live inbox, and a one-off
 * flash from the last action. Without a configured bank service the queue is the built-in
 * preview: every case starts at "not requested" and sending is off.
 */
import type { AstroCookies } from 'astro';
import {
  ALIBI_API_ORIGIN,
  ALIBI_DEMO_ORIGIN,
  ALIBI_DEMO_TOKEN,
  BANK_API_ORIGIN,
  BANK_A_TOKEN,
  BANK_SESSION_SECRET,
} from 'astro:env/server';

import { demoState, type DemoState } from '@alibi/ui/demo';

import { analyst as demoAnalyst, bank, cases, type FraudCase } from '@/data/cases';

import { bankTarget } from './bankApi';
import { alibiState, visibleCases, type AlibiState } from './caseState';
import { loadInbox } from './load';
import { RECIPIENT, backendHeaders, takeFlash, type Flash } from './screens';
import { SESSION_COOKIE, verifySession } from './session';

export interface Queue {
  /** True when the bank service answered; false in the preview. */
  live: boolean;
  /** Why the queue is a preview, when it is. */
  previewReason: string | null;
  states: Record<string, AlibiState>;
  flash: Flash | null;
  /** The cases on screen. In the live demo, Maya's case appears once her payout is flagged. */
  cases: FraudCase[];
  /** The live demo's state, when one is connected. */
  demo: DemoState | null;
}

export type QueueLoad = { redirect: string } | { queue: Queue };

export async function loadQueue(cookies: AstroCookies): Promise<QueueLoad> {
  const analyst = await verifySession(
    BANK_SESSION_SECRET,
    cookies.get(SESSION_COOKIE)?.value,
    Date.now(),
  );
  const target = bankTarget(BANK_API_ORIGIN, ALIBI_API_ORIGIN);
  if (target.origin && !analyst) return { redirect: '/sign-in/' };

  const who = { id: RECIPIENT, name: bank.name, analyst_name: analyst ?? demoAnalyst.name };
  const [loaded, demo] = await Promise.all([
    target.origin
      ? loadInbox(target, who, backendHeaders(BANK_A_TOKEN))
      : Promise.resolve({
          source: 'fixture' as const,
          reason: 'The bank service is not connected here.',
        }),
    demoState({ origin: ALIBI_DEMO_ORIGIN, token: ALIBI_DEMO_TOKEN }),
  ]);
  const live = loaded.source === 'live';
  const inbox = loaded.source === 'live' ? loaded.data : null;

  const states: Record<string, AlibiState> = {};
  const shown = visibleCases(cases, demo);
  for (const c of shown) states[c.id] = alibiState(c, inbox);

  return {
    queue: {
      live,
      previewReason: live ? null : (('reason' in loaded ? loaded.reason : null) ?? null),
      states,
      flash: takeFlash(cookies),
      cases: shown,
      demo,
    },
  };
}
