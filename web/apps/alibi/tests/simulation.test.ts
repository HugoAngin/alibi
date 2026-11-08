/**
 * `/simulate/` keeps Home and request links inside the simulation, and leaves every other app link
 * (Account, merchants, exports, actions) on `/app/`, which needs sign-in.
 */
import { describe, expect, it } from 'vitest';

import { isSimulationRoute, simulationLinks } from '../src/lib/simulation';

describe('simulationLinks', () => {
  it('keeps Home and request links in the simulation, anchors included', () => {
    expect(simulationLinks('<a href="/app/">Home</a>')).toBe('<a href="/simulate/">Home</a>');
    expect(simulationLinks('<a href="/app/requests/rfc-0001-01/">x</a>')).toBe(
      '<a href="/simulate/requests/rfc-0001-01/">x</a>',
    );
    expect(simulationLinks('<a href="/app/requests/rfc-0001-01/#step-sof">x</a>')).toBe(
      '<a href="/simulate/requests/rfc-0001-01/#step-sof">x</a>',
    );
  });

  it('leaves Account, merchants, exports and form actions alone', () => {
    const html =
      '<a href="/app/account/"></a><a href="/app/merchants/"></a>' +
      '<a href="/app/requests/rfc-0001-01/export.json"></a><form action="/app/act"></form>';
    expect(simulationLinks(html)).toBe(html);
  });
});

describe('isSimulationRoute', () => {
  it('matches only the simulation tree', () => {
    expect(isSimulationRoute('/simulate/')).toBe(true);
    expect(isSimulationRoute('/simulate/requests/rfc-0001-01/')).toBe(true);
    expect(isSimulationRoute('/app/')).toBe(false);
    expect(isSimulationRoute('/simulated')).toBe(false);
  });
});
