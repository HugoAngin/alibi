/**
 * Where a visitor is in the recorded demo: one cookie for the whole site, so that a step taken in
 * one part (withdrawing on the marketplace at /shop/, requesting at the bank at /bank/, approving
 * as Maya at /simulate/) shows in the others.
 *
 * The value is not signed: it holds nothing secret and only ever selects a recorded state, so
 * editing it can show nothing the recording does not already contain.
 */
import type { Recording, SimState } from './backend';

export const SIM_COOKIE = 'alibi_sim';
/** A visitor's demo is kept for a week of inactivity. */
export const SIM_MAX_AGE = 7 * 24 * 60 * 60;

const enc = new TextEncoder();
const dec = new TextDecoder();

function toBase64Url(s: string): string {
  let bin = '';
  for (const b of enc.encode(s)) bin += String.fromCharCode(b);
  return btoa(bin).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

function fromBase64Url(s: string): string {
  const bin = atob(s.replaceAll('-', '+').replaceAll('_', '/'));
  return dec.decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
}

export function encodeState(rec: Recording, state: SimState): string {
  return toBase64Url(JSON.stringify({ v: rec.id, n: state.node, t: state.texts }));
}

/** The visitor's state, or the start of the demo for a missing, foreign or stale cookie. */
export function decodeState(rec: Recording, value: string | undefined): SimState {
  const start: SimState = { node: rec.root, texts: {} };
  if (!value || value.length > 4096) return start;
  try {
    const v = JSON.parse(fromBase64Url(value)) as { v?: unknown; n?: unknown; t?: unknown };
    if (v.v !== rec.id || typeof v.n !== 'string' || !rec.nodes[v.n]) return start;
    const texts: Record<string, string> = {};
    if (typeof v.t === 'object' && v.t !== null) {
      for (const [k, s] of Object.entries(v.t as Record<string, unknown>)) {
        if (/^__SIM_[A-Za-z0-9._:-]{1,140}__$/.test(k) && typeof s === 'string')
          texts[k] = s.slice(0, 500);
      }
    }
    return { node: v.n, texts };
  } catch {
    return start;
  }
}
