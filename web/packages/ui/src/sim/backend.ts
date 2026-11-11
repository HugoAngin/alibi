/**
 * The demo's backend, replayed from a recording (scripts/record-simulation.mjs) instead of the
 * Python services. The recording is a graph: each node is a backend state, holding what every
 * route the sites read answered in that state, and each edge is one of the sites' actions, holding
 * what the backend answered and which state it led to. A visitor's position in the graph lives in
 * a cookie (./state.ts), so every visitor runs their own copy of the demo and nothing is shared.
 *
 * `simulatedFetch` answers the same paths the sites call on the real services (holder app, bank
 * service, demo director), so the sites' loaders, converters and refusal handling run unchanged.
 * Pure apart from the state object it updates, so it is unit tested directly.
 */

/** A recorded answer: status and body. */
interface Body {
  type: string;
  text: string;
}

export interface RecordedNode {
  label: string;
  /** Path -> [status, body id]. */
  get: Record<string, [number, string]>;
  /** Action key -> [status, body id, next node]. */
  post: Record<string, [number, string, string]>;
}

export interface Recording {
  id: string;
  root: string;
  nodes: Record<string, RecordedNode>;
  bodies: Record<string, Body>;
}

export interface SimState {
  node: string;
  /** Text the visitor typed, by placeholder (the analyst's reasons, the follow-up question). */
  texts: Record<string, string>;
}

/** The origin the sites use for every backend call while simulated. Never resolved. */
export const SIM_ORIGIN = 'https://backend.simulation.invalid';

export const QUESTION = '__SIM_QUESTION__';
export const reasonFor = (requestId: string) => `__SIM_REASON_${requestId}__`;

/** Used if a placeholder has no text (a cookie from before the text existed). */
const FALLBACK: Record<string, string> = { question: 'Please show the evidence for this payment.' };
const fallbackFor = (placeholder: string) =>
  placeholder === QUESTION ? FALLBACK.question! : 'Recorded in the simulation.';

export const NOT_IN_SIMULATION = {
  code: 'not_in_simulation',
  reason:
    'This step is not part of the recorded demo, so nothing changed. Reset the demo to start again.',
};

const MAX_TEXT = 500;
const ID = '[A-Za-z0-9][A-Za-z0-9._:-]{0,127}';
const REQUEST_ACTION = new RegExp(`^/requests/(${ID})/(run|approve|dispatch|restore)$`);
const REMOVE_ACTION = new RegExp(`^/requests/(${ID})/remove/(record|txn)/(${ID})$`);
const DISPOSITION = new RegExp(`^/packages/(${ID})/disposition$`);

export function initialState(rec: Recording): SimState {
  return { node: rec.root, texts: {} };
}

function json(status: number, value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** The visitor's text, escaped for use inside a JSON string. */
function escaped(text: string): string {
  return JSON.stringify(text).slice(1, -1);
}

function fill(text: string, texts: Record<string, string>): string {
  if (!text.includes('__SIM_')) return text;
  return text.replace(/__SIM_(?:QUESTION|REASON_[A-Za-z0-9._:-]+?)__/g, (ph) =>
    escaped(texts[ph] ?? fallbackFor(ph)),
  );
}

function answer(rec: Recording, status: number, bodyId: string, texts: SimState['texts']) {
  const body = rec.bodies[bodyId];
  if (!body)
    return json(500, {
      code: 'recording_incomplete',
      reason: 'The recording is missing an answer.',
    });
  return new Response(fill(body.text, texts), {
    status,
    headers: { 'Content-Type': body.type },
  });
}

async function readBody(init: RequestInit | undefined): Promise<Record<string, unknown>> {
  const raw = init?.body;
  if (raw === undefined || raw === null) return {};
  if (raw instanceof URLSearchParams) return Object.fromEntries(raw);
  if (typeof raw === 'string') {
    try {
      const v: unknown = JSON.parse(raw);
      return typeof v === 'object' && v !== null ? (v as Record<string, unknown>) : {};
    } catch {
      return Object.fromEntries(new URLSearchParams(raw));
    }
  }
  return {};
}

const text = (v: unknown): string => (typeof v === 'string' ? v.trim().slice(0, MAX_TEXT) : '');

/** The request this package answers, from the state's bank inbox. */
function requestOfPackage(rec: Recording, node: RecordedNode, packageId: string): string | null {
  const hit = node.get['/inbox'];
  const body = hit && rec.bodies[hit[1]];
  if (!body) return null;
  try {
    const inbox = JSON.parse(body.text) as {
      packages?: { package_id: string; request_id: string | null }[];
    };
    return inbox.packages?.find((p) => p.package_id === packageId)?.request_id ?? null;
  } catch {
    return null;
  }
}

export interface PlannedAction {
  key: string;
  /** Text to remember when the action is taken, by placeholder. */
  texts: Record<string, string>;
}

/** Which recorded action a POST is: the same keys the recorder uses. */
export function actionFor(
  rec: Recording,
  state: SimState,
  path: string,
  body: Record<string, unknown>,
): PlannedAction | null {
  if (path === '/demo/withdraw') return { key: 'withdraw', texts: {} };
  if (path === '/demo/request' || path === '/rfc') return { key: 'request', texts: {} };

  const req = REQUEST_ACTION.exec(path);
  if (req) return { key: `${req[2]}:${req[1]}`, texts: {} };
  const rm = REMOVE_ACTION.exec(path);
  if (rm) return { key: `remove:${rm[1]}:${rm[2]}/${rm[3]}`, texts: {} };

  const disp = DISPOSITION.exec(path);
  const node = rec.nodes[state.node];
  if (disp && node) {
    const requestId = requestOfPackage(rec, node, disp[1]!);
    if (!requestId) return null;
    const decision = text(body.decision);
    const evidence = Array.isArray(body.requested_evidence)
      ? text(body.requested_evidence[0])
      : text(body.requested_evidence);
    if (decision === 'request_more' && evidence)
      return { key: `followup:${requestId}`, texts: { [QUESTION]: evidence } };
    if (!decision) return null;
    const reason = text(body.reason);
    return {
      key: `decide:${requestId}:${decision}`,
      texts: reason ? { [reasonFor(requestId)]: reason } : {},
    };
  }
  return null;
}

/** The director's state body, with a version that changes whenever the visitor's state does. */
function demoStateAnswer(rec: Recording, state: SimState): Response {
  const node = rec.nodes[state.node]!;
  const hit = node.get['/demo/state'];
  const body = hit && rec.bodies[hit[1]];
  let value: Record<string, unknown> = {};
  try {
    value = body ? (JSON.parse(body.text) as Record<string, unknown>) : {};
  } catch {
    value = {};
  }
  return json(200, { ...value, version: simVersion(rec, state) });
}

/** Short, stable digest of the visitor's state: the sites reload when it changes. */
export function simVersion(rec: Recording, state: SimState): string {
  const input = `${rec.id}|${state.node}|${JSON.stringify(Object.entries(state.texts).sort())}`;
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `${rec.id}-${state.node}-${h.toString(16)}`;
}

/** A `fetch` that answers from the recording and moves `state` along it. */
export function simulatedFetch(
  rec: Recording,
  state: SimState,
  onChange: () => void = () => {},
): typeof fetch {
  return async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const method = (
      init?.method ?? (input instanceof Request ? input.method : 'GET')
    ).toUpperCase();
    if (!rec.nodes[state.node]) {
      state.node = rec.root;
      state.texts = {};
    }
    const node = rec.nodes[state.node]!;

    if (method === 'GET' || method === 'HEAD') {
      if (url.pathname === '/demo/state') return demoStateAnswer(rec, state);
      const hit = node.get[url.pathname];
      if (!hit) return json(404, { code: 'not_found', reason: 'no such item' });
      return answer(rec, hit[0], hit[1], state.texts);
    }

    if (url.pathname === '/demo/reset') {
      state.node = rec.root;
      state.texts = {};
      onChange();
      const answered = demoStateAnswer(rec, state);
      return json(200, { ok: true, reset: true, ...((await answered.json()) as object) });
    }

    const planned = actionFor(rec, state, url.pathname, await readBody(init));
    const edge = planned ? node.post[planned.key] : undefined;
    if (!planned || !edge) return json(409, NOT_IN_SIMULATION);
    const [status, bodyId, next] = edge;
    const texts = { ...state.texts, ...planned.texts };
    if (next !== state.node || Object.keys(planned.texts).length > 0) {
      state.node = next;
      state.texts = texts;
      onChange();
    }
    return answer(rec, status, bodyId, texts);
  };
}
