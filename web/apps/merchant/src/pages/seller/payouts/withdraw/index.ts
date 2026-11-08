/**
 * POST /seller/payouts/withdraw/: Maya withdraws her £2,160 seller balance to her bank. In the
 * live demo the director moves every service's clock to the payout landing in her bank, where
 * both her Alibi and Bank A's monitoring flag it. Same-origin form posts only.
 */
import type { APIRoute } from 'astro';

import { demoStep } from '@alibi/ui/demo';

import { demoTarget, setWithdrawMessage } from '@/lib/demo';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies, redirect, url }) => {
  if (request.headers.get('origin') !== url.origin)
    return new Response('Cross-site request refused.', { status: 403 });
  const form = await request.formData();
  const back = form.get('return_to') === '/seller/' ? '/seller/' : '/seller/payouts/';
  const result = await demoStep(demoTarget(), 'withdraw');
  setWithdrawMessage(
    cookies,
    result.kind === 'ok'
      ? 'Withdrawal sent. £2,160.00 is on its way to your bank account ending 00.'
      : result.kind === 'refused'
        ? `Not withdrawn: ${result.refusal.reason}`
        : result.message,
  );
  return redirect(back, 303);
};
