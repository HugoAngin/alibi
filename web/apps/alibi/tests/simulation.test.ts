/**
 * `/simulate/` keeps Home, request, Account and passport links inside the simulation, and leaves
 * every other app link (merchants, profile, exports, actions) on `/app/`, which needs sign-in.
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

  it('keeps Account and the passport page in the simulation', () => {
    expect(simulationLinks('<a href="/app/account/">Account</a>')).toBe(
      '<a href="/simulate/account/">Account</a>',
    );
    expect(simulationLinks('<a href="/app/account/passport/">x</a>')).toBe(
      '<a href="/simulate/account/passport/">x</a>',
    );
  });

  it('leaves merchants, profile, exports and form actions alone', () => {
    const html =
      '<a href="/app/merchants/"></a><a href="/app/account/profile/"></a>' +
      '<a href="/app/account/export.json"></a>' +
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
