/**
 * The simulated marketplace. SYNTHETIC. Names and sales follow the team's
 * canonical fixture: Maya (`maya-001`) is a seller on Mock Marketplace
 * (`mock-marketplace`), and her sales below are case 1's order confirmations
 * (`agents/data/out/receipts/receipts.json`, rcpt-01 to rcpt-04).
 * tests/shop.test.ts fails if they drift. The merchant ID is the one Alibi's
 * connection registry knows (apps/alibi/src/data/merchants.ts).
 */

export const shop = {
  merchantId: 'mock-marketplace',
  name: 'Mock Marketplace',
  tagline: 'Pre-loved clothes, sold by people like you.',
} as const;

/** What a product tile draws (components/ProductArt.astro). No photos: everything is synthetic. */
export type ArtKind =
  | 'shirt'
  | 'jumper'
  | 'jacket'
  | 'coat'
  | 'trousers'
  | 'skirt'
  | 'dress'
  | 'tote'
  | 'shoes'
  | 'boots'
  | 'scarf';

/** The signed-in seller on the account pages. */
export const seller = { name: 'Maya', subjectRef: 'maya-001' } as const;

export interface Sale {
  /** Order reference, as on the order confirmation. */
  reference: string;
  receiptId: string;
  title: string;
  priceMinor: number;
  sellerFeeMinor: number;
  soldOn: string;
  art: ArtKind;
}

export const sales: Sale[] = [
  {
    reference: 'MM-48001',
    receiptId: 'rcpt-01',
    title: 'Vintage denim jacket',
    priceMinor: 30000,
    sellerFeeMinor: 750,
    soldOn: '2026-10-05',
    art: 'jacket',
  },
  {
    reference: 'MM-48017',
    receiptId: 'rcpt-02',
    title: 'Wool coat, size 10',
    priceMinor: 25000,
    sellerFeeMinor: 625,
    soldOn: '2026-10-06',
    art: 'coat',
  },
  {
    reference: 'MM-48042',
    receiptId: 'rcpt-03',
    title: 'Leather boots and two scarves',
    priceMinor: 40000,
    sellerFeeMinor: 1000,
    soldOn: '2026-10-07',
    art: 'boots',
  },
  {
    reference: 'MM-48107',
    receiptId: 'rcpt-04',
    title: 'Bundle: 5 summer dresses',
    priceMinor: 45000,
    sellerFeeMinor: 1125,
    soldOn: '2026-10-11',
    art: 'dress',
  },
];

export type Category = 'tops' | 'bottoms' | 'outerwear' | 'shoes' | 'accessories';

export const categories: { id: Category; label: string }[] = [
  { id: 'tops', label: 'Tops' },
  { id: 'bottoms', label: 'Bottoms' },
  { id: 'outerwear', label: 'Outerwear' },
  { id: 'shoes', label: 'Shoes' },
  { id: 'accessories', label: 'Accessories' },
];

/**
 * Items for sale from other sellers. SYNTHETIC and not part of case 1: the
 * checkout shows an example buyer (not Maya) buying these, labelled as such.
 * Second-hand items are one-offs, so a basket holds each at most once.
 */
export interface Listing {
  id: string;
  title: string;
  category: Category;
  size: string;
  condition: 'Like new' | 'Very good' | 'Good';
  priceMinor: number;
  seller: string;
  description: string;
  art: ArtKind;
  /** Tile background behind the drawing. */
  tone: string;
}

export const listings: Listing[] = [
  {
    id: 'linen-shirt',
    title: 'Linen shirt, white',
    category: 'tops',
    size: 'M',
    condition: 'Very good',
    priceMinor: 3500,
    seller: 'juniper.lane',
    description: 'Relaxed fit, mother-of-pearl buttons. Washed and pressed, no marks.',
    art: 'shirt',
    tone: '#ece8e1',
  },
  {
    id: 'canvas-tote',
    title: 'Canvas tote bag',
    category: 'accessories',
    size: 'One size',
    condition: 'Like new',
    priceMinor: 2000,
    seller: 'oskar.vintage',
    description: 'Heavy cotton canvas with an inside pocket. Used twice.',
    art: 'tote',
    tone: '#e4e9e2',
  },
  {
    id: 'merino-jumper',
    title: 'Merino crew jumper, navy',
    category: 'tops',
    size: 'L',
    condition: 'Very good',
    priceMinor: 4200,
    seller: 'thrift.tess',
    description: 'Fine-knit merino. No bobbling, cuffs and hem still hold their shape.',
    art: 'jumper',
    tone: '#e1e5ee',
  },
  {
    id: 'trench-coat',
    title: 'Trench coat, stone',
    category: 'outerwear',
    size: '12',
    condition: 'Like new',
    priceMinor: 8500,
    seller: 'juniper.lane',
    description: 'Double-breasted with a belt and storm flap. Dry cleaned.',
    art: 'coat',
    tone: '#eee8dc',
  },
  {
    id: 'cord-trousers',
    title: 'Corduroy trousers, rust',
    category: 'bottoms',
    size: 'W30 L32',
    condition: 'Good',
    priceMinor: 2800,
    seller: 'oskar.vintage',
    description: 'Wide-wale cord, straight leg. Light wear at the hems.',
    art: 'trousers',
    tone: '#f0e3d8',
  },
  {
    id: 'leather-loafers',
    title: 'Leather loafers, brown',
    category: 'shoes',
    size: 'UK 7',
    condition: 'Good',
    priceMinor: 4800,
    seller: 'thrift.tess',
    description: 'Penny loafers, resoled this year. Polished, some creasing on the toe.',
    art: 'shoes',
    tone: '#e9e1db',
  },
  {
    id: 'silk-scarf',
    title: 'Silk scarf, floral print',
    category: 'accessories',
    size: 'One size',
    condition: 'Like new',
    priceMinor: 1800,
    seller: 'juniper.lane',
    description: 'Hand-rolled edges, 90 cm square. Never worn.',
    art: 'scarf',
    tone: '#f2e4e6',
  },
  {
    id: 'denim-skirt',
    title: 'Denim midi skirt',
    category: 'bottoms',
    size: '10',
    condition: 'Very good',
    priceMinor: 2600,
    seller: 'thrift.tess',
    description: 'Mid-wash, A-line with a front split. Fits true to size.',
    art: 'skirt',
    tone: '#dfe6ec',
  },
];

export function findListing(id: string): Listing | undefined {
  return listings.find((l) => l.id === id);
}

/**
 * The basket a first-time visitor starts with, so the checkout demo works from
 * a cold start. Once the visitor changes the basket, theirs is used instead.
 */
export const defaultBasket = ['linen-shirt', 'canvas-tote'];

/** The example buyer prefilled at checkout. Not Maya, and labelled as an example. */
export const exampleBuyer = {
  email: 'alex.example@example.com',
  name: 'Alex Morgan',
  line1: '14 Example Road',
  city: 'Leeds',
  postcode: 'LS1 4AB',
} as const;

export const deliveryOptions = [
  { id: 'standard', label: 'Standard', detail: '3–5 working days', priceMinor: 299 },
  { id: 'express', label: 'Express', detail: 'Next working day', priceMinor: 499 },
] as const;

export type DeliveryId = (typeof deliveryOptions)[number]['id'];

const gbp = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' });

export function formatGBP(minor: number): string {
  return gbp.format(minor / 100);
}
