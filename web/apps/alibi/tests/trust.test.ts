import { describe, expect, it } from 'vitest';

import { isAudience, parseTrusted, withTrust } from '../src/lib/trust';

describe('trusted banks', () => {
  it('reads bank ids from the cookie and ignores anything malformed', () => {
    expect([...parseTrusted('bank_a,bank_b')]).toEqual(['bank_a', 'bank_b']);
    expect([...parseTrusted('bank_a,,<script>,BANK_C,../x')]).toEqual(['bank_a']);
    expect(parseTrusted(undefined).size).toBe(0);
  });

  it('trusts and untrusts one bank at a time, in a stable order', () => {
    expect(withTrust(new Set(['bank_b']), 'bank_a', true)).toBe('bank_a,bank_b');
    expect(withTrust(new Set(['bank_a', 'bank_b']), 'bank_a', false)).toBe('bank_b');
    expect(withTrust(new Set(['bank_a']), 'Bad Id', true)).toBe('bank_a');
  });

  it('accepts only plain bank ids', () => {
    expect(isAudience('bank_a')).toBe(true);
    expect(isAudience('conn-bank-a-maya-001')).toBe(true);
    expect(isAudience('bank a')).toBe(false);
    expect(isAudience('')).toBe(false);
  });
});
