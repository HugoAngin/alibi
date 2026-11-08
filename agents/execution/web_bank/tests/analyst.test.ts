/**
 * The built-in analyst data (src/data/analyst_case1.json) must match the team's canonical case 1.
 * It is the output of agents.eval.api.build_analyst_preview(canonical_case()); regenerate from the
 * repo root with:
 *   uv run --frozen python -c "import json; from agents.data.api import canonical_case; \
 *   from agents.eval.api import build_analyst_preview; \
 *   json.dump(build_analyst_preview(canonical_case(), analyst_name='J. Okafor'), \
 *   open('web/apps/bank/src/data/analyst_case1.json','w'), indent=2, sort_keys=True)"
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { inboxFixture, packageFixture } from '../src/data/analyst';
import type { AnalystPackage } from '../src/lib/types';

const caseFile = new URL('../../../../agents/data/out/cases/case_0001.json', import.meta.url);
const case1 = JSON.parse(readFileSync(caseFile, 'utf8'));
const pkg = packageFixture('preview-rfc-0001-01') as AnalystPackage;

describe('analyst preview matches agents/data case 1', () => {
  it('answers Bank A request rfc-0001-01 about the canonical credit', () => {
    const rfc = case1.rfcs.find((r: { audience: string }) => r.audience === 'bank_a');
    expect(pkg.package.request_id).toBe(rfc.request_id);
    expect(pkg.package.txn_ids).toEqual(rfc.txn_ids);
    expect(pkg.package.txn_ids).toEqual(['txn-0001-004']);
  });

  it('reconciles 240000 - 18000 - 6000 = 216000 pence', () => {
    const minor = Object.fromEntries(pkg.reconciliation.lines.map((l) => [l.kind, l.amount.minor]));
    expect(minor).toEqual({ sale: 240000, refund: 18000, fee: 6000 });
    expect(pkg.reconciliation.result?.minor).toBe(216000);
    const credit = case1.bank_deliveries.find((d: { row: { txn_id: string } }) => d.row.txn_id === 'txn-0001-004').row;
    expect(credit.amount_minor).toBe(216000);
  });

  it('carries the payout timing from the case records', () => {
    const timing = pkg.evidence_by_question.find((q) => q.timing)?.timing;
    const payout = case1.merchant_lookup.find((x: { event: { kind: string } }) => x.event.kind === 'payout').event;
    expect(timing?.payout_ref).toBe(payout.payout_reference);
    expect(timing?.bank_settled_at).toBe(payout.settled_at);
    expect(timing?.settled_before_booking).toBe(true);
  });

  it('claims no verification it did not do', () => {
    expect(pkg.authentication.status).toBe('preview');
    expect(pkg.authentication.checks).toEqual([]);
    expect(inboxFixture().packages[0].authentication).toBe('preview');
  });

  it('is synthetic and names only case 1 identifiers', () => {
    const text = JSON.stringify(pkg);
    expect(text).not.toMatch(/maya-002|acct_other/);
    expect(text).toContain('txn-0001-004');
  });
});
