import { describe, expect, it } from 'vitest';

import { connectUrl, isConnectMessage, parseConnect, returnUrl } from '../src/links';

describe('connectUrl', () => {
  it('points at Alibi /connect with the merchant, context and return URL', () => {
    const href = connectUrl({
      alibiOrigin: 'http://localhost:4321',
      merchantId: 'mock-marketplace',
      returnTo: 'http://localhost:4322/account/settings/',
      context: 'account',
    });
    const url = new URL(href);
    expect(url.origin).toBe('http://localhost:4321');
    expect(url.pathname).toBe('/connect');
    expect(url.searchParams.get('merchant')).toBe('mock-marketplace');
    expect(url.searchParams.get('context')).toBe('account');
    expect(url.searchParams.get('return_to')).toBe('http://localhost:4322/account/settings/');
  });

  it('round-trips through parseConnect', () => {
    const href = connectUrl({
      alibiOrigin: 'https://alibi.example',
      merchantId: 'mock-marketplace',
      returnTo: 'https://shop.example/checkout/?order=42',
      context: 'checkout',
    });
    expect(parseConnect(new URL(href).searchParams)).toEqual({
      merchantId: 'mock-marketplace',
      context: 'checkout',
      returnTo: 'https://shop.example/checkout/?order=42',
      display: 'page',
    });
  });

  it('asks for the popup display only when told to', () => {
    const base = {
      alibiOrigin: 'https://alibi.example',
      merchantId: 'mock-marketplace',
      returnTo: 'https://shop.example/checkout/',
      context: 'checkout' as const,
    };
    expect(new URL(connectUrl(base)).searchParams.has('display')).toBe(false);
    const popup = new URL(connectUrl({ ...base, display: 'popup' }));
    expect(parseConnect(popup.searchParams).display).toBe('popup');
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

describe('returnUrl with a name', () => {
  it('sends the first name with connected only', () => {
    const yes = new URL(returnUrl('https://shop.example/checkout/', 'connected', 'Maya'));
    expect(yes.searchParams.get('alibi_name')).toBe('Maya');
    const no = new URL(returnUrl('https://shop.example/checkout/', 'declined', 'Maya'));
    expect(no.searchParams.has('alibi_name')).toBe(false);
  });
});

describe('isConnectMessage', () => {
  it('accepts the popup message and rejects anything else', () => {
    const ok = { source: 'alibi', type: 'connect', merchantId: 'm', outcome: 'connected' };
    expect(isConnectMessage(ok)).toBe(true);
    expect(isConnectMessage({ ...ok, name: 'Maya' })).toBe(true);
    expect(isConnectMessage({ ...ok, outcome: 'maybe' })).toBe(false);
    expect(isConnectMessage({ ...ok, source: 'other' })).toBe(false);
    expect(isConnectMessage({ ...ok, name: 42 })).toBe(false);
    expect(isConnectMessage('connected')).toBe(false);
    expect(isConnectMessage(null)).toBe(false);
  });
});
