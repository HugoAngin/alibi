import { describe, expect, it } from 'vitest';

import { forwardAction, isBackendPath, loadContext } from '../src/api';

type Shape = { ok: true };
const isShape = (v: unknown): v is Shape => (v as Shape)?.ok === true;
const fixture = () => ({ ok: true }) as Shape;

function fakeFetch(status: number, body: unknown, type = 'application/json'): typeof fetch {
  return (async () =>
    new Response(typeof body === 'string' ? body : JSON.stringify(body), {
      status,
      headers: { 'content-type': type },
    })) as typeof fetch;
}

describe('loadContext', () => {
  const base = { path: '/passport', isShape, fixture };

  it('uses the fixture when no backend is configured', async () => {
    const r = await loadContext({ ...base, origin: undefined });
    expect(r.source).toBe('fixture');
  });

  it('returns live data when the backend answers with the right JSON', async () => {
    const r = await loadContext({
      ...base,
      origin: 'http://api.test',
      fetchImpl: fakeFetch(200, { ok: true }),
    });
    expect(r).toEqual({ source: 'live', data: { ok: true } });
  });

  it('falls back when the backend serves HTML (JSON routes not merged)', async () => {
    const r = await loadContext({
      ...base,
      origin: 'http://api.test',
      fetchImpl: fakeFetch(200, '<html></html>', 'text/html'),
    });
    expect(r.source).toBe('fixture');
  });

  it('falls back when the JSON is the wrong shape', async () => {
    const r = await loadContext({
      ...base,
      origin: 'http://api.test',
      fetchImpl: fakeFetch(200, { other: 1 }),
    });
    expect(r.source).toBe('fixture');
  });

  it('falls back when the backend is unreachable', async () => {
    const down = (async () => {
      throw new TypeError('connection refused');
    }) as typeof fetch;
    const r = await loadContext({ ...base, origin: 'http://api.test', fetchImpl: down });
    expect(r.source).toBe('fixture');
  });

  it('reports a backend 404 as missing, without inventing data', async () => {
    const r = await loadContext({
      ...base,
      origin: 'http://api.test',
      fetchImpl: fakeFetch(404, {}),
    });
    expect(r.source).toBe('missing');
  });

  it('asks for JSON and forwards credentials', async () => {
    let seen: Headers | undefined;
    const spy = (async (_url: URL, init?: RequestInit) => {
      seen = new Headers(init?.headers);
      return new Response('{"ok":true}', { headers: { 'content-type': 'application/json' } });
    }) as typeof fetch;
    await loadContext({
      ...base,
      origin: 'http://api.test',
      fetchImpl: spy,
      headers: { Authorization: 'Bearer t' },
    });
    expect(seen?.get('accept')).toBe('application/json');
    expect(seen?.get('authorization')).toBe('Bearer t');
  });
});

describe('forwardAction', () => {
  const base = { path: '/requests/rfc-0001-01/approve', body: { digest: 'sha256:x' } };

  it('passes a backend refusal through verbatim', async () => {
    const refusal = {
      code: 'approval_digest_mismatch',
      reason: 'dispatch refused: the body changed after approval',
    };
    const r = await forwardAction({
      ...base,
      origin: 'http://api.test',
      fetchImpl: fakeFetch(409, refusal),
    });
    expect(r).toEqual({ kind: 'refused', refusal });
  });

  it('never invents a refusal when the backend is down or unconfigured', async () => {
    expect((await forwardAction({ ...base, origin: undefined })).kind).toBe('unavailable');
    const r = await forwardAction({
      ...base,
      origin: 'http://api.test',
      fetchImpl: fakeFetch(500, {}),
    });
    expect(r.kind).toBe('unavailable');
  });

  it('returns ok on success', async () => {
    const r = await forwardAction({
      ...base,
      origin: 'http://api.test',
      fetchImpl: fakeFetch(200, { done: true }),
    });
    expect(r).toEqual({ kind: 'ok', data: { done: true } });
  });

  it('refuses to forward anything but a backend path', async () => {
    const r = await forwardAction({
      ...base,
      path: 'https://evil.test/x',
      origin: 'http://api.test',
    });
    expect(r.kind).toBe('unavailable');
  });
});

describe('isBackendPath', () => {
  it('accepts same-backend paths only', () => {
    expect(isBackendPath('/requests/rfc-0001-01/approve')).toBe(true);
    for (const bad of [
      '//evil.test/x',
      'https://evil.test',
      'javascript:alert(1)',
      'relative',
      '/a b',
    ]) {
      expect(isBackendPath(bad)).toBe(false);
    }
  });
});
