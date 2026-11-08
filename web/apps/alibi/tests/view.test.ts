import { describe, expect, it } from 'vitest';

import { chooseView, type ViewInput } from '../src/lib/view';

const none: ViewInput = { query: null, cookie: null, chUaMobile: null, userAgent: null };

const UA = {
  iphone:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  androidPhone:
    'Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36',
  androidTablet:
    'Mozilla/5.0 (Linux; Android 15; Pixel Tablet) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
  ipad: 'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  mac: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
};

describe('chooseView', () => {
  it('serves the app to phones by user agent', () => {
    expect(chooseView({ ...none, userAgent: UA.iphone }).view).toBe('app');
    expect(chooseView({ ...none, userAgent: UA.androidPhone }).view).toBe('app');
  });

  it('serves the landing page to desktops and tablets', () => {
    for (const ua of [UA.mac, UA.ipad, UA.androidTablet]) {
      expect(chooseView({ ...none, userAgent: ua }).view).toBe('site');
    }
  });

  it('defaults to the landing page with no signal at all', () => {
    expect(chooseView(none)).toEqual({ view: 'site', reason: 'default', remember: null });
  });

  it('trusts the mobile client hint over the user agent', () => {
    expect(chooseView({ ...none, chUaMobile: '?1', userAgent: UA.mac }).view).toBe('app');
    expect(chooseView({ ...none, chUaMobile: '?0', userAgent: UA.androidPhone }).view).toBe('site');
  });

  it('lets ?view= override everything and asks to remember it', () => {
    expect(chooseView({ ...none, query: 'site', userAgent: UA.iphone })).toEqual({
      view: 'site',
      reason: 'query',
      remember: 'site',
    });
  });

  it('honours a remembered choice from the cookie', () => {
    expect(chooseView({ ...none, cookie: 'app', userAgent: UA.mac }).view).toBe('app');
  });

  it('ignores unknown query and cookie values', () => {
    const decision = chooseView({ ...none, query: 'tablet', cookie: 'x', userAgent: UA.iphone });
    expect(decision).toEqual({ view: 'app', reason: 'user-agent', remember: null });
  });
});
