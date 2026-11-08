/**
 * URL builders for the two Alibi entry points that live on other people's
 * sites. Pure functions, so the contract between the merchant, the bank and
 * Alibi is unit tested in one place (tests/links.test.ts).
 */

/** Where a merchant shows the connect button. Alibi words the consent screen by it. */
export type ConnectContext = 'account' | 'checkout';

export interface ConnectRequest {
  /** Origin of the Alibi site, e.g. `https://alibi.example` or `http://localhost:4321`. */
  alibiOrigin: string;
  /** Stable merchant identifier, as Alibi's connection registry knows it. */
  merchantId: string;
  /** Absolute URL Alibi sends the holder back to after they approve or decline. */
  returnTo: string;
  context: ConnectContext;
}

/**
 * The link behind "Connect to my Alibi". Alibi's `/connect` page reads these
 * parameters, shows the holder what the merchant will share, and returns them
 * to `returnTo` with `alibi=connected` or `alibi=declined`.
 */
export function connectUrl({ alibiOrigin, merchantId, returnTo, context }: ConnectRequest): string {
  const url = new URL('/connect', alibiOrigin);
  url.searchParams.set('merchant', merchantId);
  url.searchParams.set('context', context);
  url.searchParams.set('return_to', returnTo);
  return url.toString();
}

/** What Alibi's `/connect` page can safely read back out of a connect URL. */
export interface ParsedConnect {
  merchantId: string | null;
  context: ConnectContext;
  returnTo: string | null;
}

/**
 * Reads a connect request. `return_to` must be an absolute http(s) URL, or it
 * is dropped, so the page can never be used to bounce someone to a
 * `javascript:` or relative link.
 */
export function parseConnect(params: URLSearchParams): ParsedConnect {
  const context = params.get('context') === 'checkout' ? 'checkout' : 'account';
  return {
    merchantId: params.get('merchant') || null,
    context,
    returnTo: safeReturnUrl(params.get('return_to')),
  };
}

/** Builds the link that sends the holder back to the merchant with an outcome. */
export function returnUrl(returnTo: string, outcome: 'connected' | 'declined'): string {
  const url = new URL(returnTo);
  url.searchParams.set('alibi', outcome);
  return url.toString();
}

function safeReturnUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : null;
  } catch {
    return null;
  }
}
