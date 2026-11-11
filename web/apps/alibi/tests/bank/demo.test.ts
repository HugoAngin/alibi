/** The live demo decides when Maya's case is on the bank's queue (src/lib/caseState.ts). */
import { describe, expect, it } from 'vitest';

import type { DemoState } from '@alibi/ui/demo';

import { cases } from '../../src/bank/data/cases';
import { visibleCases } from '../../src/bank/lib/caseState';

const state = (flagged: boolean): DemoState => ({
  version: 'v',
  stage: flagged ? 'flagged' : 'ready',
  as_of: '2026-10-25T00:00:00Z',
  steps: { withdrawn: flagged, flagged, requested: false, received: false },
  request_id: 'rfc-0001-01',
  package_id: null,
  credit_txn: 'txn-0001-004',
  flagged_at: '2026-10-29T08:00:00Z',
});
const maya = cases.find((c) => c.request)!;

describe('visibleCases', () => {
  it('shows every case without a live demo', () => {
    expect(visibleCases(cases, null)).toEqual(cases);
  });

  it('hides Maya’s case until her payout is flagged', () => {
    const shown = visibleCases(cases, state(false));
    expect(shown.some((c) => c.id === maya.id)).toBe(false);
    expect(shown.length).toBe(cases.length - 1);
  });

  it('opens Maya’s case at the flag’s time once flagged', () => {
    const c = visibleCases(cases, state(true)).find((x) => x.id === maya.id);
    expect(c?.openedAt).toBe('2026-10-29T08:00:00Z');
  });
});
