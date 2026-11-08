#!/usr/bin/env node
/**
 * One command for the presenter's laptop: the three Astro apps, pointed at the local backend.
 *
 *   cd web && pnpm demo                  # live: app 8100, merchant 8101, bank 8102
 *   cd web && ALIBI_REPLAY=1 pnpm demo   # recorded replay: every page shows a replay banner
 *
 * Opens nothing itself; prints the three local addresses. Each app falls back to its built-in
 * case 1 data and says so if its backend is not answering, so a missing service never shows a
 * blank screen. Overrides: ALIBI_APP_URL, ALIBI_MERCHANT_URL, ALIBI_BANK_URL, BANK_A_TOKEN,
 * ANALYST_PASSCODE, BANK_SESSION_SECRET, ALIBI_DEMO_HOST. Stop with Ctrl-C (all three stop).
 *
 * The apps listen on 127.0.0.1 only (ALIBI_DEMO_HOST may name another loopback address; anything
 * else is refused). The holder app runs in demo holder mode (apps/alibi/src/lib/demoHolder.ts):
 * no Clerk key is needed, /demo-sign-in/ signs in as Maya with a signed session cookie, and every
 * holder route checks it. Supplied credentials are never printed; a generated passcode is.
 */
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const web = join(dirname(fileURLToPath(import.meta.url)), '..');
const repo = join(web, '..');
const env = process.env;

const backend = {
  app: env.ALIBI_APP_URL ?? 'http://127.0.0.1:8100',
  merchant: env.ALIBI_MERCHANT_URL ?? 'http://127.0.0.1:8101',
  bank: env.ALIBI_BANK_URL ?? 'http://127.0.0.1:8102',
};
const replay = env.ALIBI_REPLAY === '1';

const host = env.ALIBI_DEMO_HOST ?? '127.0.0.1';
if (!/^(?:127(?:\.\d{1,3}){3}|::1|localhost)$/.test(host)) {
  console.error(`  refusing to start: ALIBI_DEMO_HOST=${host} is not a loopback address.`);
  process.exit(1);
}
const urlHost = host.includes(':') ? `[${host}]` : host;
const local = (port) => `http://${urlHost}:${port}`;

/** Bank A's bearer token and where it came from: the environment, else the bank service's local tokens file. */
function bankToken() {
  if (env.BANK_A_TOKEN) return { token: env.BANK_A_TOKEN, from: 'BANK_A_TOKEN' };
  if (env.ALIBI_BANK_A_TOKEN) return { token: env.ALIBI_BANK_A_TOKEN, from: 'ALIBI_BANK_A_TOKEN' };
  const file = join(repo, 'data', 'alibi', 'bank_tokens.json');
  if (existsSync(file)) {
    try {
      const token = JSON.parse(readFileSync(file, 'utf8')).bank_a ?? '';
      return { token, from: token ? 'data/alibi/bank_tokens.json' : '' };
    } catch {
      return { token: '', from: '' };
    }
  }
  return { token: '', from: '' };
}

const passcodeSupplied = Boolean(env.ANALYST_PASSCODE);
const passcode = env.ANALYST_PASSCODE || randomBytes(4).toString('hex');
const sessionSecret = env.BANK_SESSION_SECRET || randomBytes(32).toString('hex');
// Fresh every run, never printed: signs the demo holder's session cookie.
const holderSecret = randomBytes(32).toString('hex');
const { token, from: tokenFrom } = bankToken();

const apps = [
  {
    name: 'alibi',
    pkg: '@alibi/site',
    originVar: 'ALIBI_ORIGIN',
    port: 4321,
    env: {
      ALIBI_API_ORIGIN: backend.app,
      ALIBI_DEMO_HOLDER: '1',
      HOLDER_SESSION_SECRET: holderSecret,
    },
  },
  {
    name: 'merchant',
    pkg: '@alibi/merchant',
    originVar: 'MERCHANT_ORIGIN',
    port: 4322,
    env: { ALIBI_API_ORIGIN: backend.merchant, PUBLIC_ALIBI_ORIGIN: local(4321) },
  },
  {
    name: 'bank',
    pkg: '@alibi/bank',
    originVar: 'BANK_ORIGIN',
    port: 4323,
    env: {
      BANK_API_ORIGIN: backend.bank,
      BANK_A_TOKEN: token,
      ANALYST_PASSCODE: passcode,
      BANK_SESSION_SECRET: sessionSecret,
    },
  },
];

async function reachable(url) {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(1500) });
    return r.status < 500;
  } catch {
    return false;
  }
}

// Debugger flags in NODE_OPTIONS (e.g. --inspect) would make all three dev servers fight over one
// port, so the children start without them.
const nodeOptions = (env.NODE_OPTIONS ?? '')
  .split(/\s+/)
  .filter((o) => o && !o.startsWith('--inspect'))
  .join(' ');
const shared = { PUBLIC_ALIBI_REPLAY: replay ? '1' : '', NODE_OPTIONS: nodeOptions };
const children = [];

function stopAll(code = 0) {
  for (const c of children) if (!c.killed) c.kill('SIGINT');
  setTimeout(() => process.exit(code), 500);
}
process.on('SIGINT', () => stopAll(0));
process.on('SIGTERM', () => stopAll(0));

console.log(
  replay ? '\n  RECORDED REPLAY MODE: every page shows a replay banner.\n' : '\n  Live mode.\n',
);
for (const [name, url] of Object.entries(backend)) {
  const ok = await reachable(url);
  console.log(
    `  backend ${name.padEnd(8)} ${url}  ${ok ? 'answering' : 'NOT answering (screens fall back to built-in case 1 data)'}`,
  );
}
console.log(
  token
    ? `  bank token: from ${tokenFrom}`
    : '  bank token not found: set BANK_A_TOKEN or create data/alibi/bank_tokens.json',
);
console.log(
  passcodeSupplied
    ? '  bank analyst passcode: from ANALYST_PASSCODE'
    : `  bank analyst passcode: ${passcode} (generated for this run, local only)`,
);
console.log(`  holder sign-in: ${local(4321)}/demo-sign-in/ (as Maya, no Clerk)\n`);

for (const app of apps) {
  const child = spawn('pnpm', ['--filter', app.pkg, 'run', 'dev', '--host', host], {
    cwd: web,
    env: { ...env, ...shared, [app.originVar]: local(app.port), ...app.env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.push(child);
  const prefix = `[${app.name}]`.padEnd(11);
  const pipe = (stream, out) =>
    stream.on('data', (buf) => {
      for (const line of String(buf).split('\n')) if (line.trim()) out.write(`${prefix}${line}\n`);
    });
  pipe(child.stdout, process.stdout);
  pipe(child.stderr, process.stderr);
  child.on('exit', (code) => {
    if (code && code !== 0) {
      console.error(`${prefix}exited with ${code}; stopping the others.`);
      stopAll(code);
    }
  });
  console.log(`  ${app.name.padEnd(8)} ${local(app.port)}/`);
}
