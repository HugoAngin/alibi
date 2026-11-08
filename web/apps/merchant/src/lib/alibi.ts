/**
 * The shop's own memory of an Alibi connection. Alibi tells the shop only the
 * outcome and the holder's first name (packages/ui/src/links.ts); the shop
 * keeps that in a cookie so every page can show the connected state. A real
 * merchant would store it against the customer's account.
 */
export const ALIBI_COOKIE = 'mm_alibi';

export interface AlibiLink {
  name: string;
  connectedAt: string;
}

export function parseAlibiLink(value: string | undefined): AlibiLink | null {
  if (!value) return null;
  try {
    const v = JSON.parse(decodeURIComponent(value)) as Partial<AlibiLink>;
    if (typeof v.connectedAt !== 'string') return null;
    return {
      name: typeof v.name === 'string' ? v.name.slice(0, 40) : '',
      connectedAt: v.connectedAt,
    };
  } catch {
    return null;
  }
}

export function serializeAlibiLink(link: AlibiLink): string {
  return encodeURIComponent(JSON.stringify(link));
}
