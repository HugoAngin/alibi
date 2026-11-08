import { describe, expect, it } from 'vitest';

import { defaultBasket, listings } from '../src/data/shop';
import { parseAlibiLink, serializeAlibiLink } from '../src/lib/alibi';
import {
  addToBasket,
  buyerProtectionMinor,
  parseBasket,
  parseOrder,
  priceBasket,
  removeFromBasket,
  serializeBasket,
} from '../src/lib/basket';

describe('basket cookie', () => {
  it('starts a first visit on the default basket, and keeps an emptied one empty', () => {
    expect(parseBasket(undefined)).toEqual(defaultBasket);
    expect(parseBasket('')).toEqual([]);
    expect(parseBasket(serializeBasket([]))).toEqual([]);
  });

  it('drops unknown and repeated ids', () => {
    expect(parseBasket('linen-shirt.nope.linen-shirt.silk-scarf')).toEqual([
      'linen-shirt',
      'silk-scarf',
    ]);
  });

  it('adds each one-off item once and removes it', () => {
    let ids = addToBasket([], 'trench-coat');
    ids = addToBasket(ids, 'trench-coat');
    ids = addToBasket(ids, 'not-a-listing');
    expect(ids).toEqual(['trench-coat']);
    expect(parseBasket(serializeBasket(ids))).toEqual(ids);
    expect(removeFromBasket(ids, 'trench-coat')).toEqual([]);
  });
});

describe('pricing', () => {
  it('charges 5% plus 70p buyer protection, nothing on an empty basket', () => {
    expect(buyerProtectionMinor(5500)).toBe(345);
    expect(buyerProtectionMinor(0)).toBe(0);
  });

  it('adds items, protection and the chosen delivery', () => {
    const p = priceBasket(['linen-shirt', 'canvas-tote'], 'express');
    expect(p.subtotalMinor).toBe(5500);
    expect(p.deliveryMinor).toBe(499);
    expect(p.totalMinor).toBe(5500 + 345 + 499);
    expect(priceBasket(['linen-shirt']).deliveryMinor).toBe(0);
    expect(priceBasket([], 'standard').totalMinor).toBe(0);
  });

  it('has unique listing ids', () => {
    expect(new Set(listings.map((l) => l.id)).size).toBe(listings.length);
  });
});

describe('placed order cookie', () => {
  it('round-trips and rejects junk', () => {
    const order = {
      ref: 'MM-51234',
      ids: ['linen-shirt'],
      delivery: 'standard' as const,
      name: 'Alex Morgan',
      email: 'alex.example@example.com',
      totalMinor: 4012,
      placedAt: '2026-11-08T10:00:00.000Z',
    };
    expect(parseOrder(JSON.stringify(order))).toEqual(order);
    expect(parseOrder('{oops')).toBeNull();
    expect(parseOrder(JSON.stringify({ ref: 1 }))).toBeNull();
    expect(parseOrder(undefined)).toBeNull();
  });
});

describe('Alibi connection cookie', () => {
  it('round-trips the first name and rejects junk', () => {
    const link = { name: 'Maya', connectedAt: '2026-11-08T10:00:00.000Z' };
    expect(parseAlibiLink(serializeAlibiLink(link))).toEqual(link);
    expect(parseAlibiLink('%7Boops')).toBeNull();
    expect(parseAlibiLink(encodeURIComponent('{"name":"x"}'))).toBeNull();
    expect(parseAlibiLink(undefined)).toBeNull();
  });
});
