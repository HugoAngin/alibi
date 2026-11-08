import { describe, expect, it } from 'vitest';

import { day, money, ref, when } from '../src/lib/format';

describe('money', () => {
  it('formats integer pence', () => {
    expect(money({ minor: 216000, currency: 'GBP' })).toBe('£2,160.00');
    expect(money({ minor: -65000, currency: 'GBP' })).toBe('-£650.00');
  });
});

describe('when', () => {
  it('reads a UTC timestamp without time-zone drift', () => {
    expect(when('2026-10-28T06:30:00Z')).toBe('28 Oct 2026, 06:30 UTC');
    expect(when('2026-11-02T09:00:00.123Z')).toBe('2 Nov 2026, 09:00 UTC');
  });

  it('leaves anything else alone', () => {
    expect(when('not a time')).toBe('not a time');
    expect(when('2026-10-28T06:30:00+01:00')).toBe('2026-10-28T06:30:00+01:00');
  });
});

describe('day and ref', () => {
  it('formats short forms', () => {
    expect(day('2026-10-28T06:30:00Z')).toBe('28 Oct');
    expect(ref('rec-0001-021', 1)).toBe('rec-0001-021 r1');
  });
});
