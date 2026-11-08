// Captures the screenshots the deck uses, in light mode, and crops each to
// the part of the screen its slide is about. Writes assets/*.png.
//
// Needs a Chromium browser (EDGE or CHROME env var, default Microsoft Edge on
// macOS), the Astro sites running (`cd web && pnpm dev`), and the s4a template
// previews rendered (`uv run --frozen python -m alibi.app.preview_render` on the
// s4a branch) in S4A_PREVIEW_DIR.
//
// The s4a previews use the template sample data, not case 1. Slides that show
// them say so; replace them with captures of the wired app once it runs case 1.

import { spawnSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import sharp from 'sharp';

const browser =
  process.env.EDGE ??
  process.env.CHROME ??
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge';
const preview = process.env.S4A_PREVIEW_DIR;
if (!preview) throw new Error('Set S4A_PREVIEW_DIR to the s4a ui_preview folder.');
const profile = mkdtempSync(join(tmpdir(), 'deck-shots-'));

// name, url, window [w, h], crop {left, top, width, height} or null for the whole window
const shots = [
  ['landing', 'http://localhost:4321/', [1280, 720], null],
  ['phone', 'http://localhost:4321/phone/', [1280, 960], { left: 425, top: 70, width: 430, height: 880 }],
  ['merchant_connect', 'http://localhost:4322/account/settings/', [1280, 560], { left: 64, top: 0, width: 760, height: 560 }],
  ['bank_case', 'http://localhost:4323/cases/FC-0147/', [1280, 470], null],
  ['trace', `file://${preview}/request_detail.html`, [1280, 5000], { left: 276, top: 1195, width: 728, height: 765 }],
  ['review', `file://${preview}/request_detail.html`, [1280, 5000], { left: 276, top: 4105, width: 728, height: 845 }],
  ['analyst_checks', `file://${preview}/analyst_package.html`, [1280, 2000], { left: 276, top: 480, width: 728, height: 975 }],
  ['refused', `file://${preview}/dispatch_refused.html`, [1280, 800], { left: 276, top: 100, width: 728, height: 660 }],
  ['what_changed', `file://${preview}/transaction_detail.html`, [1280, 3000], { left: 276, top: 2330, width: 728, height: 350 }],
];

for (const [name, url, [w, h], crop] of shots) {
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
