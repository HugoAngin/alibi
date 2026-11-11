#!/usr/bin/env node
/**
 * Records the live demo once, so the three sites can replay it with no backend, no Gemini key and
 * no Modal (packages/ui/src/sim/). Run from web/:
 *
 *   pnpm record:simulation
 *
 * Starts the real Python stack (alibi.demo: holder app, mock merchant, mock bank, director) in a
 * temporary folder on ports 8150-8153, in agents mode with recorded Gemini answers
 * (ALIBI_REPLAY=1, alibi/fixtures/replays/), so no key is needed. It then walks every path the
 * sites can take through Maya's case and saves what each screen's backend route answered at each
 * step: a graph whose nodes are backend states and whose edges are the sites' actions (withdraw,
 * request, run, approve, send, remove and restore an item, the analyst's decisions, the
 * follow-up). Output: packages/ui/src/sim/recording.json. Nothing is written elsewhere and the
 * stack is stopped at the end.
 *
 * Free text the visitor types (the analyst's reasons, the follow-up question) is recorded as a
 * placeholder and filled in per visitor at replay time.
 */
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { clearTimeout, setTimeout } from 'node:timers';
import { fileURLToPath } from 'node:url';

const web = join(dirname(fileURLToPath(import.meta.url)), '..');
const repo = join(web, '..');
const OUT = join(web, 'packages', 'ui', 'src', 'sim', 'recording.json');
const PORTS = { app: 8150, merchant: 8151, bank: 8152, director: 8153 };
const BASE = Object.fromEntries(
  Object.entries(PORTS).map(([k, p]) => [k, `http://127.0.0.1:${p}`]),
);
/** Placeholders for text the visitor types; packages/ui/src/sim/backend.ts fills them in. */
const TEXT = {
  reason: (rid) => `__SIM_REASON_${rid}__`,
  question: '__SIM_QUESTION__',
};
const MAIN = 'rfc-0001-01';

// ------------------------------------------------------------------ the stack

const tmp = mkdtempSync(join(tmpdir(), 'alibi-sim-'));
const stack = spawn(
  'uv',
  [
    ...['run', '--frozen', 'python', '-m', 'alibi.demo', '--case', '1', '--reset', '--replay'],
    ...['--root', join(tmp, 'root'), '--keys', join(tmp, 'keys')],
    ...['--tokens', join(tmp, 'tokens.json'), '--timeout', '3600'],
    ...['--app-port', PORTS.app, '--merchant-port', PORTS.merchant].map(String),
    ...['--bank-port', PORTS.bank, '--director-port', PORTS.director].map(String),
  ],
  {
    cwd: repo,
    env: { ...process.env, ALIBI_MODE: 'agents', ALIBI_REPLAY: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true,
  },
);
let stackLog = '';
stack.stderr.on('data', (b) => (stackLog += b));

function stop(code) {
  try {
    process.kill(-stack.pid, 'SIGTERM');
  } catch {
    // already gone
  }
  rmSync(tmp, { recursive: true, force: true });
  process.exit(code);
}
process.on('SIGINT', () => stop(130));

await new Promise((resolve, reject) => {
  let out = '';
  const timer = setTimeout(() => reject(new Error('the stack did not start in 120 s')), 120_000);
  stack.stdout.on('data', (b) => {
    out += b;
    if (out.includes('"director"')) {
      clearTimeout(timer);
      resolve();
    }
  });
  stack.on('exit', (code) => reject(new Error(`the stack exited (${code}):\n${stackLog}`)));
}).catch((e) => {
  console.error(e.message);
  stop(1);
});

const tokens = JSON.parse(readFileSync(join(tmp, 'tokens.json'), 'utf8'));
const AUTH = {
  app: {},
  bank: { Authorization: `Bearer ${tokens.bank_a}` },
  director: { Authorization: `Bearer ${tokens.admin}` },
};

// ------------------------------------------------------------------ HTTP

/** Absolute links the backend hands out point at this temporary stack: make them relative. */
const localhost = /http:\/\/127\.0\.0\.1:81[0-9]{2}/g;

async function call(service, method, path, body) {
  const r = await fetch(BASE[service] + path, {
    method,
    headers: {
      Accept: 'application/json',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...AUTH[service],
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(180_000),
  });
  const text = (await r.text()).replace(localhost, '');
  return { status: r.status, type: r.headers.get('content-type') ?? 'application/json', text };
}

const json = (res) => JSON.parse(res.text);

// ------------------------------------------------------------------ recording

const bodies = {};
function store(res) {
  const hash = createHash('sha256').update(res.type).update('\0').update(res.text).digest('hex');
  const id = hash.slice(0, 16);
  bodies[id] = { type: res.type.split(';')[0], text: res.text };
  return id;
}

/** Everything the sites read, at the current state, keyed by the path the sites ask for. */
async function snapshot() {
  const get = {};
  const take = async (service, path) => {
    const res = await call(service, 'GET', path);
    get[path] = res;
    return res;
  };
  const passport = json(await take('app', '/passport'));
  for (const r of passport.requests ?? []) {
    await take('app', `/requests/${r.request_id}`);
    await take('app', `/requests/${r.request_id}/export.json`);
  }
  for (const t of passport.transactions ?? []) await take('app', `/txn/${t.txn_id}`);
  const inbox = json(await take('bank', '/inbox'));
  for (const p of inbox.packages ?? []) await take('bank', `/packages/${p.package_id}`);
  await take('director', '/demo/state');
  return get;
}

/** Random IDs and the director's digest differ between runs of the same state. */
const normal = (text) =>
  text
    .replace(/\b[a-z]{2,8}_[0-9a-f]{12,}\b/g, 'ID')
    .replace(/"version":\s*"[0-9a-f]{16}"/g, '"version":"V"');

function differences(a, b) {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...keys].filter((k) => !a[k] || !b[k] || normal(a[k].text) !== normal(b[k].text));
}

// ------------------------------------------------------------------ actions

async function packageFor(rid) {
  const inbox = json(await call('bank', 'GET', '/inbox'));
  const pkg = [...inbox.packages].reverse().find((p) => p.request_id === rid);
  if (!pkg) throw new Error(`no package for ${rid}`);
  return pkg.package_id;
}

/** One site action, by its stable key (packages/ui/src/sim/backend.ts derives the same keys). */
async function perform(key) {
  const [verb, rid, ...rest] = key.split(':');
  switch (verb) {
    case 'withdraw':
      return call('director', 'POST', '/demo/withdraw', {});
    case 'request':
      return call('director', 'POST', '/demo/request', { recipient: 'bank_a' });
    case 'run':
      return call('app', 'POST', `/requests/${rid}/run`, {});
    case 'approve': {
      const a = json(await call('app', 'GET', `/requests/${rid}`)).review.approval;
      const body = { digest: a.digest, audience: a.audience, policy_version: a.policy_version };
      return call('app', 'POST', `/requests/${rid}/approve`, body);
    }
    case 'dispatch':
      return call('app', 'POST', `/requests/${rid}/dispatch`, {});
    case 'remove':
      return call('app', 'POST', `/requests/${rid}/remove/${rest.join(':')}`, {});
    case 'restore':
      return call('app', 'POST', `/requests/${rid}/restore`, {});
    case 'decide':
      return call('bank', 'POST', `/packages/${await packageFor(rid)}/disposition`, {
        decision: rest[0],
        reason: TEXT.reason(rid),
      });
    case 'followup':
      return call('bank', 'POST', `/packages/${await packageFor(rid)}/disposition`, {
        decision: 'request_more',
        reason: `Follow-up requested: ${TEXT.question}`,
        requested_evidence: [TEXT.question],
      });
    default:
      throw new Error(`unknown action ${key}`);
  }
}

// ------------------------------------------------------------------ the walk

const nodes = {};
const byPath = new Map();
let at = null; // the path the stack is at now
let resets = 0;

async function goTo(path) {
  const here = at ?? [];
  const prefix = at !== null && here.length <= path.length && here.every((k, i) => k === path[i]);
  if (!prefix) {
    const r = await call('director', 'POST', '/demo/reset', {});
    if (r.status !== 200) throw new Error(`reset failed: ${r.status} ${r.text.slice(0, 200)}`);
    resets++;
    at = [];
  }
  for (const key of path.slice(at.length)) {
    const res = await perform(key);
    if (res.status >= 500)
      throw new Error(`${key} failed: ${res.status} ${res.text.slice(0, 300)}`);
    at = [...at, key];
  }
}

/** Records the state at `path`, and the edge from its parent. */
async function visit(path, label) {
  const id = `n${Object.keys(nodes).length}`;
  const parent = path.length ? byPath.get(path.slice(0, -1).join(' ')) : null;
  let res = null;
  if (parent) {
    await goTo(path.slice(0, -1));
    res = await perform(path.at(-1));
    at = path;
  } else {
    await goTo(path);
  }
  const get = await snapshot();
  nodes[id] = { label, get, post: {} };
  byPath.set(path.join(' '), id);
  if (parent) nodes[parent].post[path.at(-1)] = [res.status, store(res), id];
  process.stdout.write(`  ${id.padEnd(5)} ${label}  (${res ? res.status : 'start'})\n`);
  return { id, get };
}

/** An action whose result is a state already recorded: record its answer, check, and link. */
async function link(path, targetPath, note) {
  const parent = byPath.get(path.slice(0, -1).join(' '));
  const target = byPath.get(targetPath.join(' '));
  await goTo(path.slice(0, -1));
  const res = await perform(path.at(-1));
  at = path;
  const diff = differences(await snapshot(), nodes[target].get);
  nodes[parent].post[path.at(-1)] = [res.status, store(res), target];
  process.stdout.write(
    `  ${parent.padEnd(5)} ${path.at(-1)} -> ${target} ${note}  (${res.status})` +
      (diff.length ? `  differs from ${target} in ${diff.join(', ')}` : '') +
      '\n',
  );
}

const started = Date.now();
console.log('  recording Maya’s case (case 1, agents mode, recorded Gemini answers)');
const A = `approve:${MAIN}`;
const S = `dispatch:${MAIN}`;
const main = ['withdraw', 'request', `run:${MAIN}`];

await visit([], 'ready: payout in the seller balance');
await visit(['withdraw'], 'flagged: payout withdrawn, credit flagged');
await visit(['withdraw', 'request'], 'requested: Bank A asked for Maya’s alibi');
const investigated = await visit(main, 'investigated');
// Running the investigation again redoes the same work: the same state.
await link([...main, `run:${MAIN}`], main, '(run again)');
await visit([...main, A], 'approved');
await visit([...main, A, S], 'sent: Bank A received the package');
for (const d of ['clear', 'escalate']) {
  await visit([...main, A, S, `decide:${MAIN}:${d}`], `bank decision: ${d}`);
}
// "Request more" without a question is refused by the bank and changes nothing.
await link([...main, A, S, `decide:${MAIN}:request_more`], [...main, A, S], '(refused)');

// The follow-up: Bank A asks one more question, Maya answers it.
const follow = [...main, A, S, `followup:${MAIN}`];
const { get: afterFollow } = await visit(follow, 'bank follow-up requested');
const followId = (json(afterFollow['/passport']).requests ?? []).find(
  (r) => r.parent_request_id === MAIN,
)?.request_id;
if (followId) {
  const f = [...follow, `run:${followId}`];
  await visit(f, 'follow-up investigated');
  await visit([...f, `approve:${followId}`], 'follow-up approved');
  await visit([...f, `approve:${followId}`, `dispatch:${followId}`], 'follow-up sent');
  for (const d of ['clear', 'escalate']) {
    const p = [...f, `approve:${followId}`, `dispatch:${followId}`, `decide:${followId}:${d}`];
    await visit(p, `follow-up decision: ${d}`);
  }
}

// Maya removes one item from the review: before approving, and after (which refuses the send).
const removable = (json(investigated.get[`/requests/${MAIN}`]).review.included ?? [])
  .filter((i) => i.removable && i.remove_action)
  .map((i) => i.remove_action.split('/remove/')[1]);
for (const ref of removable) {
  const X = `remove:${MAIN}:${ref}`;
  const U = `restore:${MAIN}`;
  await visit([...main, X], `removed ${ref}`);
  await link([...main, X, U], main, '(restored)');
  await visit([...main, X, A], `removed ${ref}, approved`);
  await visit([...main, X, A, S], `removed ${ref}, sent`);
  for (const d of ['clear', 'escalate']) {
    await visit([...main, X, A, S, `decide:${MAIN}:${d}`], `removed ${ref}, sent, ${d}`);
  }
  await visit([...main, A, X], `approved, then removed ${ref}`);
  await link([...main, A, X, S], [...main, A, X], '(send refused)');
  await link([...main, A, X, U], main, '(restored)');
  await link([...main, A, X, A], [...main, X, A], '(approved again)');
}

// ------------------------------------------------------------------ output

const out = { root: 'n0', bodies: {}, nodes: {} };
for (const [id, n] of Object.entries(nodes)) {
  const get = {};
  for (const [path, res] of Object.entries(n.get)) get[path] = [res.status, store(res)];
  out.nodes[id] = { label: n.label, get, post: n.post };
}
out.bodies = Object.fromEntries(Object.entries(bodies).sort(([a], [b]) => a.localeCompare(b)));
const content = JSON.stringify({ nodes: out.nodes, bodies: out.bodies });
const recording = {
  id: createHash('sha256').update(content).digest('hex').slice(0, 12),
  case: 1,
  mode: 'agents, recorded Gemini answers (ALIBI_REPLAY=1)',
  root: out.root,
  nodes: out.nodes,
  bodies: out.bodies,
};
writeFileSync(OUT, JSON.stringify(recording) + '\n');
const kb = Math.round(JSON.stringify(recording).length / 1024);
console.log(
  `  ${Object.keys(nodes).length} states, ${Object.keys(bodies).length} distinct answers, ` +
    `${kb} KB, ${resets} resets, ${Math.round((Date.now() - started) / 1000)} s -> ${OUT}`,
);
stop(0);
