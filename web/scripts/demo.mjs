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
 * ANALYST_PASSCODE, BANK_SESSION_SECRET. Stop with Ctrl-C (all three stop).
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

/** Bank A's bearer token: the environment, else the bank service's local tokens file. */
function bankToken() {
  if (env.BANK_A_TOKEN) return env.BANK_A_TOKEN;
  if (env.ALIBI_BANK_A_TOKEN) return env.ALIBI_BANK_A_TOKEN;
  const file = join(repo, 'data', 'alibi', 'bank_tokens.json');
  if (existsSync(file)) {
    try {
      return JSON.parse(readFileSync(file, 'utf8')).bank_a ?? '';
    } catch {
      return '';
    }
  }
  return '';
}

const passcode = env.ANALYST_PASSCODE ?? randomBytes(4).toString('hex');
const sessionSecret = env.BANK_SESSION_SECRET ?? randomBytes(32).toString('hex');
const token = bankToken();

const apps = [
  {
    name: 'alibi',
    pkg: '@alibi/site',
    port: 4321,
    env: { ALIBI_API_ORIGIN: backend.app },
  },
  {
    name: 'merchant',
    pkg: '@alibi/merchant',
    port: 4322,
    env: { ALIBI_API_ORIGIN: backend.merchant, PUBLIC_ALIBI_ORIGIN: 'http://localhost:4321' },
  },
  {
    name: 'bank',
    pkg: '@alibi/bank',
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
if (!token)
  console.log('  bank token not found: set BANK_A_TOKEN or create data/alibi/bank_tokens.json');
console.log(
  `  bank analyst passcode: ${passcode}${env.ANALYST_PASSCODE ? '' : ' (generated for this run)'}\n`,
);

for (const app of apps) {
  const child = spawn('pnpm', ['--filter', app.pkg, 'dev'], {
    cwd: web,
    env: { ...env, ...shared, ...app.env },
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
  console.log(`  ${app.name.padEnd(8)} http://localhost:${app.port}/`);
}
