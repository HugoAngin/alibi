/**
 * The live demo from the marketplace's side (alibi/demo_director.py): whether Maya's payout is
 * still in her seller balance (the demo's start) and the one-off message after she withdraws.
 */
import type { AstroCookies } from 'astro';
import { ALIBI_DEMO_ORIGIN, ALIBI_DEMO_TOKEN } from 'astro:env/server';

import { demoState, type DemoState, type DemoTarget } from '@alibi/ui/demo';

export const demoTarget = (): DemoTarget => ({
  origin: ALIBI_DEMO_ORIGIN,
  token: ALIBI_DEMO_TOKEN,
});

export interface PayoutDemo {
  state: DemoState | null;
  /** True while the payout sits in the seller balance and can be withdrawn. */
  available: boolean;
  message: string | null;
}

const FLASH = 'mm_withdraw';

export async function payoutDemo(cookies: AstroCookies): Promise<PayoutDemo> {
  const state = await demoState(demoTarget());
  const message = cookies.get(FLASH)?.value ?? null;
  if (message) cookies.delete(FLASH, { path: '/' });
  return { state, available: state?.stage === 'ready', message };
}

export function setWithdrawMessage(cookies: AstroCookies, message: string): void {
  cookies.set(FLASH, message.slice(0, 300), {
    path: '/',
    httpOnly: true,
    sameSite: 'lax',
    maxAge: 60,
  });
}
