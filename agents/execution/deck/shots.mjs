// Captures the screenshots the deck uses, in light mode, and crops each to
// the part of the screen its slide is about. Writes assets/*.png.
//
// Needs a Chromium browser (EDGE or CHROME env var; default Google Chrome, else
// Microsoft Edge, on macOS) and the sites: local (`cd web && pnpm dev`) or the
// deployed Workers via ALIBI_URL, MERCHANT_URL and BANK_URL, e.g.
//   ALIBI_URL=https://www.alibi-app.workers.dev BANK_URL=https://bank.alibi-app.workers.dev npm run shots
// The s4a template previews are captured only when S4A_PREVIEW_DIR is set
// (`uv run --frozen python -m alibi.app.preview_render` on the s4a branch).
//
// The s4a previews use the template sample data, not case 1. Slides that show
// them say so; replace them with captures of the wired app once it runs case 1.

import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import sharp from 'sharp';

const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const edge = '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge';
const browser = process.env.EDGE ?? process.env.CHROME ?? (existsSync(chrome) ? chrome : edge);
const preview = process.env.S4A_PREVIEW_DIR;
const alibi = (process.env.ALIBI_URL ?? 'http://localhost:4321').replace(/\/$/, '');
const merchant = (process.env.MERCHANT_URL ?? 'http://localhost:4322').replace(/\/$/, '');
const bank = (process.env.BANK_URL ?? 'http://localhost:4323').replace(/\/$/, '');
const profile = mkdtempSync(join(tmpdir(), 'deck-shots-'));

// name, url, window [w, h], crop {left, top, width, height} or null for the whole window
const shots = [
  ['landing', `${alibi}/`, [1280, 720], null],
  ['phone', `${alibi}/phone/`, [1280, 960], { left: 425, top: 70, width: 430, height: 880 }],
  ['merchant_connect', `${merchant}/account/settings/`, [1280, 560], { left: 64, top: 0, width: 760, height: 560 }],
  ['bank_case', `${bank}/cases/FC-0147/`, [1280, 470], null],
  // Execution's analyst console (agents/execution/web_bank, built-in case 1 preview until live).
  ['bank_inbox', `${bank}/inbox/`, [1280, 720], null],
  ['bank_package', `${bank}/packages/preview-rfc-0001-01/`, [1280, 1400], null],
  ['trace', `file://${preview}/request_detail.html`, [1280, 5000], { left: 276, top: 1195, width: 728, height: 765 }],
  ['review', `file://${preview}/request_detail.html`, [1280, 5000], { left: 276, top: 4105, width: 728, height: 845 }],
  ['analyst_checks', `file://${preview}/analyst_package.html`, [1280, 2000], { left: 276, top: 480, width: 728, height: 975 }],
  ['refused', `file://${preview}/dispatch_refused.html`, [1280, 800], { left: 276, top: 100, width: 728, height: 660 }],
  ['what_changed', `file://${preview}/transaction_detail.html`, [1280, 3000], { left: 276, top: 2330, width: 728, height: 350 }],
];

for (const [name, url, [w, h], crop] of shots) {
  if (url.startsWith('file://undefined')) {
    console.log(`skip ${name}: S4A_PREVIEW_DIR not set, keeping assets/${name}.png`);
    continue;
  }
  const raw = join(profile, `${name}.png`);
  const run = spawnSync(browser, [
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    `--user-data-dir=${profile}`,
    '--blink-settings=preferredColorScheme=1',
    '--virtual-time-budget=4000',
    `--window-size=${w},${h}`,
    `--screenshot=${raw}`,
    url,
  ]);
  if (run.status !== 0) throw new Error(`${name}: browser exited ${run.status}`);
  const img = sharp(raw);
  await (crop ? img.extract(crop) : img).png().toFile(`assets/${name}.png`);
  console.log(`assets/${name}.png`);
}
