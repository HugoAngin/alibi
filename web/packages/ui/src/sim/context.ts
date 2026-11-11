/**
 * The simulation for the request being served. Each site's middleware runs the request inside
 * `runSimulated` when the site is simulated (ALIBI_SIMULATION=1); the backend clients in ../api.ts
 * and ../demo.ts then ask `currentSimulation()` and answer from the recording instead of the
 * network. Outside a simulated request it returns null and nothing changes.
 *
 * AsyncLocalStorage keeps one visitor's state apart from another's on the same Worker isolate
 * (Cloudflare Workers support it with the nodejs_compat flag, which every site sets).
 */
import { AsyncLocalStorage } from 'node:async_hooks';
import type { AstroCookies } from 'astro';

import { simulatedFetch, type Recording, type SimState } from './backend';
import { SIM_COOKIE, SIM_MAX_AGE, decodeState, encodeState } from './state';
import recording from './recording.json';

export const RECORDING = recording as Recording;

export interface Simulation {
  recording: Recording;
  state: SimState;
  fetch: typeof fetch;
}

const store = new AsyncLocalStorage<Simulation>();

export function currentSimulation(): Simulation | null {
  return store.getStore() ?? null;
}

export function isSimulated(): boolean {
  return store.getStore() !== undefined;
}

/** What the middleware hands over: Astro's cookies and the request URL. */
interface SimulatedRequest {
  cookies: AstroCookies;
  url: URL;
}

/**
 * Serve one request against the visitor's copy of the demo. If an action moved the demo on, the
 * response sets the new state cookie (Astro attaches cookies after the middleware returns).
 */
export async function runSimulated(
  { cookies, url }: SimulatedRequest,
  next: () => Promise<Response>,
  opts: { recording?: Recording } = {},
): Promise<Response> {
  const rec = opts.recording ?? RECORDING;
  const state = decodeState(rec, cookies.get(SIM_COOKIE)?.value);
  let changed = false;
  const sim: Simulation = {
    recording: rec,
    state,
    fetch: simulatedFetch(rec, state, () => {
      changed = true;
    }),
  };
  const response = await store.run(sim, next);
  if (changed) {
    cookies.set(SIM_COOKIE, encodeState(rec, state), {
      path: '/',
      maxAge: SIM_MAX_AGE,
      sameSite: 'lax',
      httpOnly: true,
      secure: url.protocol === 'https:',
    });
  }
  return response;
}
