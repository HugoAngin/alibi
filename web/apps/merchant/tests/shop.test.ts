import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { sales, seller, shop } from '../src/data/shop';

// The team's canonical fixture, owned by `data`. Read-only.
const receiptsFile = new URL('../../../../agents/data/out/receipts/receipts.json', import.meta.url);
const receipts = JSON.parse(readFileSync(receiptsFile, 'utf8')).receipts as {
  id: string;
  reference: string;
  issuer: string;
  document_date: string;
  total_minor: number;
  items: { description: string; amount_minor: number }[];
  extra_fields: { seller_fee_minor?: number; seller?: string };
}[];

describe("Maya's sales match case 1's order confirmations", () => {
  it('copies reference, item, price, fee, date and seller exactly', () => {
    for (const sale of sales) {
      const r = receipts.find((x) => x.id === sale.receiptId)!;
      expect(r.issuer).toBe(shop.name);
      expect([r.reference, r.document_date, r.total_minor]).toEqual([
        sale.reference,
        sale.soldOn,
        sale.priceMinor,
      ]);
      expect(r.items[0].description).toBe(sale.title);
      expect(r.extra_fields.seller_fee_minor).toBe(sale.sellerFeeMinor);
      expect(r.extra_fields.seller).toBe(seller.subjectRef);
    }
  });
});
