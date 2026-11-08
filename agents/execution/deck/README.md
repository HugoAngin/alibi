# Deck v2 (staged by execution)

Built from `agents/research/deck/` (research's v1) at research's request (04:32Z). Writes `agents/execution/out/alibi_deck.pptx`. Plan, placeholder registry and rules: `agents/research/out/DECK.md`; every number eval has not verified stays an amber `{{PH-nn}}`.

```bash
cd agents/execution/deck && npm install && npm run build      # pptxgenjs, sharp
npm run shots   # optional, retake screenshots (see below)
```

Not built on this machine yet: npm packages are not installed here (waiting on the human's OK). `node --check` passes on both scripts.

## What changed from v1

| Slide | Change | Source |
| --- | --- | --- |
| 2b (new) | "Three surfaces, one passport": landing, Maya's app, Bank A console | `web/docs/hosting.md`, `agents/execution/web_bank` |
| 4 Data | 80 cases in 12 families (second platform added), gold 54 · 12 · 14; data's own chart replaces the drawn bars | `agents/data/out/index.json`, `agents/data/out/figures/fig1_families.png` |
| 9 Live refusal | Fourth card: six bytes changed in a signed package, rejected by the bank verifier; bank-verifier reference run 8 of 8, labelled reference, not live | `out/TAMPER_DEMO.md`, results record `fdd4570242` |
| 10 Oversight | Bank A console package view when captured (`bank_package`), else the s4a preview captioned as sample data | `agents/execution/web_bank` |
| 15 What could fool us | Eval's six rows replace the four drafted threats | `agents/eval/out/METHOD.md` |
| 17 Limits | Encryption at rest only holds with the key outside the data folder (SP-47) | `out/at_rest_probe.py`, record `dfb59fcdbf` |

`assets/landing.png` was retaken from https://www.alibi-app.workers.dev on 2026-11-08 04:35Z (new design; retake once data's hero lands).

## Screenshots still to take

`shots.mjs` now reads `ALIBI_URL`, `MERCHANT_URL`, `BANK_URL` (default localhost), adds `bank_inbox` and `bank_package`, defaults to Google Chrome, and skips the s4a previews when `S4A_PREVIEW_DIR` is unset. Once research deploys the console:

```bash
ALIBI_URL=https://www.alibi-app.workers.dev BANK_URL=https://bank.alibi-app.workers.dev MERCHANT_URL=https://merchant.alibi-app.workers.dev npm run shots
```

Until `bank_inbox.png` and `bank_package.png` exist, slides 2b and 10 fall back to `bank_case.png` and `analyst_checks.png` with honest captions.

## Placeholders still open

All lockbox numbers (PH-03 to PH-05, PH-10 to PH-12, PH-15 to PH-45) wait on eval's frozen run. PH-01 (final commit, links) is lead's. PH-08 is filled from eval's record bc1a8728a3 (native signed-correction fixture).
