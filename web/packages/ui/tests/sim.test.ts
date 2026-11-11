import type { AstroCookies } from 'astro';
import { describe, expect, it } from 'vitest';

import { loadContext } from '../src/api';
import { demoEnabled, demoState } from '../src/demo';
import {
  NOT_IN_SIMULATION,
  QUESTION,
  initialState,
  simulatedFetch,
  type SimState,
} from '../src/sim/backend';
import { RECORDING, currentSimulation, runSimulated } from '../src/sim/context';
import { guideStep } from '../src/sim/guide';
import { SIM_COOKIE, decodeState, encodeState } from '../src/sim/state';

const rec = RECORDING;
const BASE = 'https://backend.simulation.invalid';
const MAIN = 'rfc-0001-01';

function visitor() {
  const state: SimState = initialState(rec);
  let changes = 0;
  const f = simulatedFetch(rec, state, () => changes++);
  const get = async (path: string) => f(new URL(path, BASE));
  const getJson = async <T = Record<string, unknown>>(path: string) =>
    (await (await get(path)).json()) as T;
  const post = async (path: string, body: Record<string, unknown> | URLSearchParams = {}) =>
    f(new URL(path, BASE), {
      method: 'POST',
      body: body instanceof URLSearchParams ? body : JSON.stringify(body),
    });
  const stage = async () => (await getJson<{ stage: string }>('/demo/state')).stage;
  return { state, get, getJson, post, stage, changes: () => changes };
}

async function toReceived(v: ReturnType<typeof visitor>) {
  await v.post('/demo/withdraw');
  await v.post('/demo/request', { recipient: 'bank_a' });
  await v.post(`/requests/${MAIN}/run`, new URLSearchParams());
  await v.post(`/requests/${MAIN}/approve`, new URLSearchParams({ digest: 'x' }));
  await v.post(`/requests/${MAIN}/dispatch`, new URLSearchParams());
  const inbox = await v.getJson<{ packages: { package_id: string; request_id: string }[] }>(
    '/inbox',
  );
  return inbox.packages.find((p) => p.request_id === MAIN)!.package_id;
}

describe('the recording', () => {
  it('starts before the withdrawal, with every answer present', () => {
    expect(rec.nodes[rec.root]).toBeDefined();
    for (const node of Object.values(rec.nodes)) {
      for (const [, body] of Object.values(node.get)) expect(rec.bodies[body]).toBeDefined();
      for (const [, body, next] of Object.values(node.post)) {
        expect(rec.bodies[body]).toBeDefined();
        expect(rec.nodes[next]).toBeDefined();
      }
    }
  });

  it('holds no address of the machine it was recorded on', () => {
    for (const body of Object.values(rec.bodies)) expect(body.text).not.toMatch(/127\.0\.0\.1/);
  });
});

describe('simulatedFetch: the demo, step by step', () => {
  it('walks withdraw, request, investigate, approve and send', async () => {
    const v = visitor();
    expect(await v.stage()).toBe('ready');
    expect((await v.post('/demo/withdraw')).status).toBe(200);
    expect(await v.stage()).toBe('flagged');
    expect((await v.post('/demo/request', { recipient: 'bank_a' })).status).toBe(201);
    expect(await v.stage()).toBe('requested');

    const passport = await v.getJson<{ requests: { request_id: string }[] }>('/passport');
    expect(passport.requests.map((r) => r.request_id)).toContain(MAIN);

    const run = await v.post(`/requests/${MAIN}/run`, new URLSearchParams());
    expect(run.status).toBe(200);
    const detail = await v.getJson<{ investigation: { status: string } }>(`/requests/${MAIN}`);
    expect(detail.investigation.status).toBe('complete');

    expect((await v.post(`/requests/${MAIN}/approve`, new URLSearchParams())).status).toBe(200);
    const sent = await v.post(`/requests/${MAIN}/dispatch`, new URLSearchParams());
    expect(sent.status).toBe(200);
    expect(((await sent.json()) as { outcome: string }).outcome).toBe('success');
    expect(await v.stage()).toBe('received');
    expect((await v.get(`/requests/${MAIN}/export.json`)).status).toBe(200);
    expect(v.changes()).toBe(5);
  });

  it('puts the analyst’s own reason into the bank’s record, escaped', async () => {
    const v = visitor();
    const pkg = await toReceived(v);
    const reason = 'Matches the "signed" payout — fine';
    const r = await v.post(`/packages/${pkg}/disposition`, { decision: 'clear', reason });
    expect(r.status).toBe(200);
    const view = await v.getJson<{ dispositions: { decision: string; reason: string }[] }>(
      `/packages/${pkg}`,
    );
    expect(view.dispositions.at(-1)).toMatchObject({ decision: 'clear', reason });
  });

  it('carries the analyst’s follow-up question to Maya', async () => {
    const v = visitor();
    const pkg = await toReceived(v);
    const question = 'Please show the settlement report for October.';
    const r = await v.post(`/packages/${pkg}/disposition`, {
      decision: 'request_more',
      reason: `Follow-up requested: ${question}`,
      requested_evidence: [question],
    });
    expect(r.status).toBe(200);
    expect(v.state.texts[QUESTION]).toBe(question);
    const passport = await v.getJson<{ requests: { request_id: string }[] }>('/passport');
    const follow = passport.requests.find((x) => x.request_id !== MAIN)!;
    expect(JSON.stringify(await v.getJson(`/requests/${follow.request_id}`))).toContain(question);
  });

  it('refuses a send after an item is removed from an approved package, then re-approves', async () => {
    const v = visitor();
    await v.post('/demo/withdraw');
    await v.post('/demo/request');
    await v.post(`/requests/${MAIN}/run`);
    await v.post(`/requests/${MAIN}/approve`);
    const detail = await v.getJson<{ review: { included: { remove_action?: string }[] } }>(
      `/requests/${MAIN}`,
    );
    const remove = detail.review.included.find((i) => i.remove_action)!.remove_action!;
    expect((await v.post(remove)).status).toBe(200);
    const refused = await v.post(`/requests/${MAIN}/dispatch`);
    expect(refused.status).toBe(409);
    expect(((await refused.json()) as { code: string }).code).toBe('approval_digest_mismatch');
    expect((await v.post(`/requests/${MAIN}/approve`)).status).toBe(200);
    expect((await v.post(`/requests/${MAIN}/dispatch`)).status).toBe(200);
    expect(await v.stage()).toBe('received');
  });

  it('refuses a step the recording does not have, and changes nothing', async () => {
    const v = visitor();
    const before = v.state.node;
    const r = await v.post(`/requests/${MAIN}/dispatch`);
    expect(r.status).toBe(409);
    expect(await r.json()).toEqual(NOT_IN_SIMULATION);
    expect(v.state.node).toBe(before);
    expect(v.changes()).toBe(0);
  });

  it('answers 404 for an item the state does not have', async () => {
    const v = visitor();
    expect((await v.get(`/requests/${MAIN}`)).status).toBe(404);
  });

  it('resets to the start and forgets typed text', async () => {
    const v = visitor();
    const pkg = await toReceived(v);
    await v.post(`/packages/${pkg}/disposition`, { decision: 'escalate', reason: 'Odd timing' });
    expect(Object.keys(v.state.texts)).not.toHaveLength(0);
    await v.post('/demo/reset');
    expect(v.state).toEqual(initialState(rec));
    expect(await v.stage()).toBe('ready');
  });

  it('gives each state its own version, so the sites reload on a change', async () => {
    const v = visitor();
    const a = (await v.getJson<{ version: string }>('/demo/state')).version;
    await v.post('/demo/withdraw');
    const b = (await v.getJson<{ version: string }>('/demo/state')).version;
    expect(a).not.toBe(b);
  });
});

describe('the state cookie', () => {
  it('round-trips the state, including text in any script', () => {
    const state = { node: Object.keys(rec.nodes)[3]!, texts: { [QUESTION]: 'Où est l’argent ✓' } };
    expect(decodeState(rec, encodeState(rec, state))).toEqual(state);
  });

  it('falls back to the start for a missing, garbled, foreign or stale value', () => {
    const start = initialState(rec);
    expect(decodeState(rec, undefined)).toEqual(start);
    expect(decodeState(rec, 'not base64 json')).toEqual(start);
    expect(decodeState({ ...rec, id: 'other' }, encodeState(rec, start))).toEqual(start);
    const unknown = encodeState(rec, { node: 'n999999', texts: {} });
    expect(decodeState(rec, unknown)).toEqual(start);
  });

  it('drops text under names that are not placeholders', () => {
    const value = encodeState(rec, { node: rec.root, texts: { evil: 'x', [QUESTION]: 'q' } });
    expect(decodeState(rec, value).texts).toEqual({ [QUESTION]: 'q' });
  });
});

function fakeCookies(initial: Record<string, string> = {}) {
  const jar = new Map(Object.entries(initial));
  const set: { name: string; value: string; opts: Record<string, unknown> }[] = [];
  const cookies = {
    get: (name: string) => (jar.has(name) ? { value: jar.get(name)! } : undefined),
    set: (name: string, value: string, opts: Record<string, unknown>) => {
      jar.set(name, value);
      set.push({ name, value, opts });
    },
  } as unknown as AstroCookies;
  return { cookies, set };
}

describe('runSimulated', () => {
  it('serves the request against the visitor’s state and saves a change', async () => {
    const { cookies, set } = fakeCookies();
    const url = new URL('https://alibi.eliohaddad.workers.dev/shop/seller/payouts/withdraw/');
    expect(currentSimulation()).toBeNull();
    await runSimulated({ cookies, url }, async () => {
      expect(demoEnabled({ origin: undefined, token: undefined })).toBe(true);
      const sim = currentSimulation()!;
      await sim.fetch(new URL('/demo/withdraw', BASE), { method: 'POST' });
      return new Response('ok');
    });
    expect(set).toHaveLength(1);
    expect(set[0]).toMatchObject({
      name: SIM_COOKIE,
      opts: { secure: true, httpOnly: true, path: '/' },
    });
    // One site: the cookie stays on its own host.
    expect(set[0]!.opts).not.toHaveProperty('domain');
    expect(decodeState(rec, set[0]!.value).node).not.toBe(rec.root);
  });

  it('sets no cookie when nothing changed, and the backend clients need no network', async () => {
    const { cookies, set } = fakeCookies();
    const url = new URL('http://localhost:4321/simulate/');
    await runSimulated({ cookies, url }, async () => {
      const loaded = await loadContext({
        origin: undefined,
        path: '/passport',
        isShape: (v): v is unknown => typeof v === 'object' && v !== null,
        fixture: () => null,
        fetchImpl: undefined,
      });
      expect(loaded.source).toBe('live');
      expect((await demoState({ origin: undefined, token: undefined }))?.stage).toBe('ready');
      return new Response('ok');
    });
    expect(set).toHaveLength(0);
  });
});

describe('the step guide', () => {
  it('points at the part of the site where the next step happens', () => {
    expect(guideStep('ready', MAIN, null)).toMatchObject({
      number: 1,
      path: '/shop/seller/payouts/',
    });
    expect(guideStep('flagged', MAIN, null)).toMatchObject({
      number: 2,
      path: '/bank/cases/FC-0147/',
    });
    expect(guideStep('requested', MAIN, null)).toMatchObject({
      number: 3,
      path: '/simulate/',
      then: { path: `/simulate/requests/${MAIN}/` },
    });
    expect(guideStep('received', MAIN, 'pkg_1')).toMatchObject({
      number: 4,
      path: '/bank/packages/pkg_1/',
    });
  });
});
