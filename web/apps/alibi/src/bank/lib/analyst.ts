/**
 * Who is at the console. On a simulated site every visitor is the demo analyst, with no passcode:
 * the console acts only on the visitor's own run of the recorded demo. Otherwise it is the analyst
 * of a valid signed session (src/lib/session.ts), or nobody.
 */
import type { AstroCookies } from 'astro';
import { BANK_SESSION_SECRET } from 'astro:env/server';

import { isSimulated } from '@alibi/ui/sim/context';

import { analyst as demoAnalyst } from '@/bank/data/cases';

import { SESSION_COOKIE, verifySession } from './session';

export async function currentAnalyst(cookies: AstroCookies): Promise<string | null> {
  if (isSimulated()) return demoAnalyst.name;
  return verifySession(BANK_SESSION_SECRET, cookies.get(SESSION_COOKIE)?.value, Date.now());
}
