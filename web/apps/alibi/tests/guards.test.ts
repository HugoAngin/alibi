import { describe, expect, it } from 'vitest';

import { asDispatch } from '../src/lib/api/guards';

const dispatch = {
  outcome: 'success',
  package_id: 'pkg-0001-01',
  request_id: 'rfc-0001-01',
  recipient: 'Bank A',
  attempted_at: '2026-11-02T09:20:00Z',
  receipt: null,
  refusal: null,
  retry_action: null,
  review_href: '/requests/rfc-0001-01',
  history_href: '/sharing',
};

describe('asDispatch', () => {
  it('accepts the dispatch context bare or wrapped', () => {
    expect(asDispatch(dispatch)?.recipient).toBe('Bank A');
    expect(asDispatch({ dispatch })?.package_id).toBe('pkg-0001-01');
  });

  it('rejects anything that is not a dispatch result', () => {
    for (const v of [
      null,
      {},
      { outcome: 'maybe', request_id: 'x' },
      { dispatch: { outcome: 'success' } },
    ]) {
      expect(asDispatch(v)).toBeNull();
    }
  });
});
