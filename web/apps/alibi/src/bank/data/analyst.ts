/**
 * Built-in analyst screens for case 1, used when the backend is not connected.
 *
 * `analyst_case1.json` is the exact output of `agents.eval.api.build_analyst_preview(canonical_case())`
 * (regenerate: see tests/analyst.test.ts). Its authentication is `preview`: no package has been
 * signed, sent or verified, and the screens say so. Nothing here invents a verification result.
 */

import type { AnalystInbox, AnalystPackage } from '@/bank/lib/types';

import preview from './analyst_case1.json';

const PACKAGE = preview as unknown as AnalystPackage;

export function packageFixture(id: string): AnalystPackage | null {
  return id === PACKAGE.package.package_id ? PACKAGE : null;
}

export function inboxFixture(): AnalystInbox {
  const counts = { supported: 0, contradicted: 0, unresolved: 0 };
  for (const c of PACKAGE.claim_status) counts[c.status] += 1;
  return {
    recipient: PACKAGE.recipient,
    packages: [
      {
        package_id: PACKAGE.package.package_id,
        request_id: PACKAGE.package.request_id,
        received_at: PACKAGE.authentication.received_at,
        expires_at: PACKAGE.package.expires_at,
        subject_ref_masked: PACKAGE.package.subject_ref_masked,
        authentication: PACKAGE.authentication.status,
        claim_counts: counts,
        disposition: PACKAGE.disposition_form.current?.disposition ?? null,
        href: `/bank/packages/${PACKAGE.package.package_id}/`,
      },
    ],
    requests_sent: [
      {
        request_id: PACKAGE.package.request_id,
        parent_request_id: null,
        sent_at: '—',
        status: 'answered',
        question_count: PACKAGE.claim_status.length,
      },
    ],
  };
}
