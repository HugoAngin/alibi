/**
 * Decides whether a request for `/` gets the product landing page or the app.
 *
 * The same URL serves both: a phone gets Alibi's app, anything else gets the
 * landing page that explains it. The decision is made on the server (the
 * Cloudflare Worker) from request headers, so there is no flash of the wrong
 * page and no client-side redirect. Pure, so it is unit tested directly in
 * tests/view.test.ts.
 */

export type View = 'site' | 'app';

/** Cookie that remembers an explicit choice made with `?view=`. */
export const VIEW_COOKIE = 'alibi_view';

/** Headers the decision reads. Sent as `Vary` so caches keep the two apart. */
export const VARY_HEADERS = ['Sec-CH-UA-Mobile', 'User-Agent', 'Cookie'] as const;

export interface ViewInput {
  /** `?view=` on the request, if any. */
  query: string | null;
  /** The `alibi_view` cookie, if any. */
  cookie: string | null;
  /** `Sec-CH-UA-Mobile`: `?1` on Chromium phones, `?0` elsewhere, absent on Safari and Firefox. */
  chUaMobile: string | null;
  userAgent: string | null;
}

export interface ViewDecision {
  view: View;
  /** Why, for debugging and for the `X-Alibi-View` response header. */
  reason: 'query' | 'cookie' | 'client-hint' | 'user-agent' | 'default';
  /** Set when the choice came from `?view=` and should be remembered. */
  remember: View | null;
}

// Phones only. Tablets (iPad, Android without "Mobile") get the landing page,
// which is responsive; the app view is designed for a phone in one hand.
const PHONE_UA = /\b(iPhone|iPod)\b|Android.*\bMobile\b|\bMobi\b|Windows Phone/i;

function asView(value: string | null): View | null {
  return value === 'app' || value === 'site' ? value : null;
}

export function chooseView({ query, cookie, chUaMobile, userAgent }: ViewInput): ViewDecision {
  const fromQuery = asView(query);
  if (fromQuery) return { view: fromQuery, reason: 'query', remember: fromQuery };

  const fromCookie = asView(cookie);
  if (fromCookie) return { view: fromCookie, reason: 'cookie', remember: null };

  if (chUaMobile === '?1') return { view: 'app', reason: 'client-hint', remember: null };
  if (chUaMobile === '?0') return { view: 'site', reason: 'client-hint', remember: null };

  if (userAgent && PHONE_UA.test(userAgent)) {
    return { view: 'app', reason: 'user-agent', remember: null };
  }
  return { view: 'site', reason: 'default', remember: null };
}
