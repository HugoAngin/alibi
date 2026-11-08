/**
 * URL builders for the two Alibi entry points that live on other people's
 * sites. Pure functions, so the contract between the merchant, the bank and
 * Alibi is unit tested in one place (tests/links.test.ts).
 */

/** Where a merchant shows the connect button. Alibi words the consent screen by it. */
export type ConnectContext = 'account' | 'checkout';

/**
 * How the consent screen is shown. `page` is a full-page redirect that comes
 * back to `returnTo`; `popup` is a small window over the merchant's page that
 * reports the outcome with `postMessage` and closes, so checkout is not lost.
 */
export type ConnectDisplay = 'page' | 'popup';

export type ConnectOutcome = 'connected' | 'declined';

export interface ConnectRequest {
  /** Origin of the Alibi site, e.g. `https://alibi.example` or `http://localhost:4321`. */
  alibiOrigin: string;
  /** Stable merchant identifier, as Alibi's connection registry knows it. */
  merchantId: string;
  /** Absolute URL Alibi sends the holder back to after they approve or decline. */
  returnTo: string;
  context: ConnectContext;
  display?: ConnectDisplay;
}

/**
 * The link behind "Connect to my Alibi". Alibi's `/connect` page reads these
 * parameters, signs the holder in, shows what the merchant will share, and
 * returns them to `returnTo` with `alibi=connected` or `alibi=declined`.
 */
export function connectUrl({
  alibiOrigin,
  merchantId,
  returnTo,
  context,
  display = 'page',
}: ConnectRequest): string {
  const url = new URL('/connect', alibiOrigin);
  url.searchParams.set('merchant', merchantId);
  url.searchParams.set('context', context);
  url.searchParams.set('return_to', returnTo);
  if (display === 'popup') url.searchParams.set('display', 'popup');
  return url.toString();
}

/** What Alibi's `/connect` page can safely read back out of a connect URL. */
export interface ParsedConnect {
  merchantId: string | null;
  context: ConnectContext;
  returnTo: string | null;
  display: ConnectDisplay;
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
    display: params.get('display') === 'popup' ? 'popup' : 'page',
  };
}

/**
 * Builds the link that sends the holder back to the merchant with an outcome.
 * `name` is the holder's first name, the one thing the consent screen says
 * the merchant will learn; it is only sent with `connected`.
 */
export function returnUrl(returnTo: string, outcome: ConnectOutcome, name?: string): string {
  const url = new URL(returnTo);
  url.searchParams.set('alibi', outcome);
  if (outcome === 'connected' && name) url.searchParams.set('alibi_name', name);
  return url.toString();
}

/** The message a `popup` consent screen posts to the merchant window that opened it. */
export interface ConnectMessage {
  source: 'alibi';
  type: 'connect';
  merchantId: string;
  outcome: ConnectOutcome;
  name?: string;
}

/** True for a well-formed {@link ConnectMessage}. The receiver must also check `event.origin`. */
export function isConnectMessage(value: unknown): value is ConnectMessage {
  if (typeof value !== 'object' || value === null) return false;
  const m = value as Record<string, unknown>;
  return (
    m.source === 'alibi' &&
    m.type === 'connect' &&
    typeof m.merchantId === 'string' &&
    (m.outcome === 'connected' || m.outcome === 'declined') &&
    (m.name === undefined || typeof m.name === 'string')
  );
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
