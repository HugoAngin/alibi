import { describe, expect, it } from 'vitest';

import { connectUrl, parseConnect, returnUrl } from '../src/links';

describe('connectUrl', () => {
  it('points at Alibi /connect with the merchant, context and return URL', () => {
    const href = connectUrl({
      alibiOrigin: 'http://localhost:4321',
      merchantId: 'resalehub',
      returnTo: 'http://localhost:4322/account/settings/',
      context: 'account',
    });
    const url = new URL(href);
    expect(url.origin).toBe('http://localhost:4321');
    expect(url.pathname).toBe('/connect');
    expect(url.searchParams.get('merchant')).toBe('resalehub');
    expect(url.searchParams.get('context')).toBe('account');
    expect(url.searchParams.get('return_to')).toBe('http://localhost:4322/account/settings/');
  });

  it('round-trips through parseConnect', () => {
    const href = connectUrl({
      alibiOrigin: 'https://alibi.example',
      merchantId: 'resalehub',
      returnTo: 'https://shop.example/checkout/?order=42',
      context: 'checkout',
    });
    expect(parseConnect(new URL(href).searchParams)).toEqual({
      merchantId: 'resalehub',
      context: 'checkout',
      returnTo: 'https://shop.example/checkout/?order=42',
    });
  });
});

describe('parseConnect', () => {
  it('drops return URLs that are not absolute http(s)', () => {
    for (const bad of ['javascript:alert(1)', '/relative', 'not a url', '']) {
      const params = new URLSearchParams({ merchant: 'm', return_to: bad });
      expect(parseConnect(params).returnTo).toBeNull();
    }
  });

  it('defaults an unknown context to account', () => {
    expect(parseConnect(new URLSearchParams({ context: 'other' })).context).toBe('account');
  });
});

describe('returnUrl', () => {
  it('adds the outcome and keeps existing parameters', () => {
    const url = new URL(returnUrl('https://shop.example/checkout/?order=42', 'declined'));
    expect(url.searchParams.get('order')).toBe('42');
    expect(url.searchParams.get('alibi')).toBe('declined');
  });
});
