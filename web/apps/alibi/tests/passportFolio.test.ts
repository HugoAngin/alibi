/**
 * The dashboard's financial passport (src/lib/passportFolio.ts) is filled only from the holder's
 * passport home: their merchant profiles become dividers, their banks become cards, and the
 * counts on the passport page and pocket add up from the same connections.
 */
import { describe, expect, it } from 'vitest';

import { passportFixture } from '../src/data/fixtures';
import { FOLIO_TINTS, folioFromPassport } from '../src/lib/passportFolio';

describe('folioFromPassport', () => {
  const data = passportFixture();
  const folio = folioFromPassport(data);
  const merchants = data.connections.filter((c) => c.kind === 'merchant');
  const banks = data.connections.filter((c) => c.kind === 'bank');

  it('shows the holder and their passport number', () => {
    expect(folio.holder).toBe(data.holder.name);
    expect(folio.subjectRef).toBe(data.holder.subject_ref);
  });

  it('gives each merchant a divider and each bank a card', () => {
    expect(folio.profiles.map((p) => p.name)).toEqual(merchants.map((m) => m.name));
    expect(folio.cards.map((c) => c.bank)).toEqual(banks.map((b) => b.name));
    expect(folio.featured?.title).toBe(merchants.at(-1)?.name);
  });

  it('keeps the front divider cream, whatever the number of merchants', () => {
    expect(folio.profiles.at(-1)?.tint).toBe(FOLIO_TINTS[3]);
  });

  it('counts records and connections from the same list', () => {
    const records = data.connections.reduce((sum, c) => sum + c.record_count, 0);
    expect(folio.pocket.title).toBe(`${records.toLocaleString('en-GB')} records`);
    expect(folio.facts[0]).toEqual({ label: 'Merchants', value: `${merchants.length} connected` });
    expect(folio.facts[1]).toEqual({ label: 'Banks', value: `${banks.length} read-only` });
  });

  it('uses a known logo and bank colours, and plain ink for anyone else', () => {
    const named = folioFromPassport({
      ...data,
      connections: [
        { ...merchants[0]!, name: 'Amazon' },
        { ...banks[0]!, name: 'NatWest' },
        { ...banks[0]!, connection_id: 'other', name: 'Bank A' },
      ],
    });
    expect(named.profiles[0]?.logo).toBe('/logos/amazon.png');
    expect(named.cards.map((c) => c.kind)).toEqual(['natwest', 'plain']);
    expect(named.cards[1]?.logo).toBeUndefined();
  });

  it('leaves out disconnected connections', () => {
    const gone = folioFromPassport({
      ...data,
      connections: data.connections.map((c) => ({ ...c, status: 'disconnected' as const })),
    });
    expect(gone.profiles).toEqual([]);
    expect(gone.cards).toEqual([]);
    expect(gone.featured).toBeUndefined();
  });
});
