/**
 * The basket and order totals. The basket lives in a cookie (ids joined by
 * `.`) so every page can render it on the server. Pure functions, unit tested
 * in tests/basket.test.ts.
 */
import {
  defaultBasket,
  deliveryOptions,
  findListing,
  type DeliveryId,
  type Listing,
} from '../data/shop';

export const BASKET_COOKIE = 'mm_basket';
export const ORDER_COOKIE = 'mm_last_order';

const MAX_ITEMS = 20;

/**
 * Reads the cookie. Absent means a first visit (the default basket). An
 * emptied basket is stored as `-`, because browsers drop empty cookie values.
 */
export function parseBasket(value: string | undefined): string[] {
  if (value === undefined) return [...defaultBasket];
  const ids = value.split('.').filter((id) => findListing(id));
  return [...new Set(ids)].slice(0, MAX_ITEMS);
}

export function serializeBasket(ids: string[]): string {
  return [...new Set(ids)].slice(0, MAX_ITEMS).join('.') || '-';
}

export function addToBasket(ids: string[], id: string): string[] {
  return findListing(id) && !ids.includes(id) ? [...ids, id] : ids;
}

export function removeFromBasket(ids: string[], id: string): string[] {
  return ids.filter((x) => x !== id);
}

/** Buyer protection, as resale marketplaces charge it: 5% of the items plus 70p. */
export function buyerProtectionMinor(subtotalMinor: number): number {
  return subtotalMinor === 0 ? 0 : Math.round(subtotalMinor * 0.05) + 70;
}

export interface Priced {
  items: Listing[];
  subtotalMinor: number;
  protectionMinor: number;
  deliveryMinor: number;
  totalMinor: number;
}

/** Prices a basket. Leave `delivery` out before the buyer has chosen one. */
export function priceBasket(ids: string[], delivery?: DeliveryId): Priced {
  const items = ids.map(findListing).filter((l): l is Listing => Boolean(l));
  const subtotalMinor = items.reduce((s, l) => s + l.priceMinor, 0);
  const protectionMinor = buyerProtectionMinor(subtotalMinor);
  const option = deliveryOptions.find((d) => d.id === delivery);
  const deliveryMinor = items.length && option ? option.priceMinor : 0;
  return {
    items,
    subtotalMinor,
    protectionMinor,
    deliveryMinor,
    totalMinor: subtotalMinor + protectionMinor + deliveryMinor,
  };
}

/** The order the confirmation page shows, kept in a cookie after "Pay". */
export interface PlacedOrder {
  ref: string;
  ids: string[];
  delivery: DeliveryId;
  name: string;
  email: string;
  totalMinor: number;
  placedAt: string;
}

export function parseOrder(value: string | undefined): PlacedOrder | null {
  if (!value) return null;
  try {
    const o = JSON.parse(value) as Partial<PlacedOrder>;
    if (typeof o.ref !== 'string' || !Array.isArray(o.ids) || typeof o.totalMinor !== 'number')
      return null;
    return {
      ref: o.ref,
      ids: o.ids.filter((id): id is string => typeof id === 'string'),
      delivery: o.delivery === 'express' ? 'express' : 'standard',
      name: typeof o.name === 'string' ? o.name : '',
      email: typeof o.email === 'string' ? o.email : '',
      totalMinor: o.totalMinor,
      placedAt: typeof o.placedAt === 'string' ? o.placedAt : '',
    };
  } catch {
    return null;
  }
}
