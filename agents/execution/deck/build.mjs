// Deck v2 (execution). Builds agents/execution/out/alibi_deck.pptx from the plan in
// agents/research/out/DECK.md. Run from agents/execution/deck: npm install && npm run build
//
// Every number that eval has not verified is a {{PH-nn}} placeholder, drawn as
// a highlighted run so it cannot be mistaken for a result. The registry in
// DECK.md says which verified record fills each one. Screenshots come from
// shots.mjs; s4a template previews show sample data and are captioned so.
//
// Restyle in one place: PALETTE and FONT below.

import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import pptxgen from 'pptxgenjs';
import sharp from 'sharp';

const OUT = '../out/alibi_deck.pptx';

const PALETTE = {
  navy: '1F3A5F', // Alibi brand, dominant on title and close
  ink: '1C1C1A',
  soft: '55554F',
  faint: '6B6B64',
  line: 'D9DCE1',
  tint: 'EEF2F7', // card fill
  white: 'FFFFFF',
  ok: '2F6B3A',
  bad: '9B2C2C',
  warn: '8A5A00',
  phFill: 'FCE7B2', // placeholder highlight
  phInk: '6B4400',
};
const FONT = { head: 'Calibri', body: 'Calibri' };

const W = 10; // LAYOUT_16x9: 10 x 5.625 in
const M = 0.5; // side margin

const pres = new pptxgen();
pres.layout = 'LAYOUT_16x9';
pres.author = 'Alibi team';
pres.title = 'Alibi: your money’s alibi, ready before anyone asks';
pres.subject = 'AgentVerse 2026 Finance Track submission deck v2 (draft with placeholders)';
pres.theme = { headFontFace: FONT.head, bodyFontFace: FONT.body };

// ---------------------------------------------------------------- layouts --

pres.defineSlideMaster({
  title: 'ALIBI_DARK',
  background: { color: PALETTE.navy },
  objects: [
    {
      placeholder: {
        options: {
          name: 'title',
          type: 'title',
          x: M,
          y: 1.55,
          w: W - 2 * M,
          h: 1.2,
          fontFace: FONT.head,
          fontSize: 44,
          bold: true,
          color: PALETTE.white,
          valign: 'bottom',
          align: 'left',
          margin: 0,
        },
        text: 'Title',
      },
    },
    {
      placeholder: {
        options: {
          name: 'body',
          type: 'body',
          x: M,
          y: 2.9,
          w: W - 2 * M,
          h: 1.6,
          fontFace: FONT.body,
          fontSize: 18,
          color: 'DCE4EE',
          valign: 'top',
          margin: 0,
        },
        text: 'Subtitle',
      },
    },
  ],
});

pres.defineSlideMaster({
  title: 'ALIBI_CONTENT',
  background: { color: PALETTE.white },
  margin: [0.4, M, 0.45, M],
  slideNumber: { x: W - M - 0.4, y: 5.25, w: 0.4, h: 0.25, fontSize: 10, color: PALETTE.faint, align: 'right' },
  objects: [
    {
      text: {
        text: 'Alibi · AgentVerse 2026 Finance Track · synthetic data only',
        options: { x: M, y: 5.25, w: 6, h: 0.25, fontSize: 10, color: PALETTE.faint, margin: 0 },
      },
    },
    {
      placeholder: {
        options: {
          name: 'title',
          type: 'title',
          x: M,
          y: 0.62,
          w: W - 2 * M,
          h: 0.6,
          fontFace: FONT.head,
          fontSize: 28,
          bold: true,
          color: PALETTE.ink,
          valign: 'top',
          align: 'left',
          margin: 0,
        },
        text: 'Title',
      },
    },
  ],
});

// ---------------------------------------------------------------- helpers --

/** Splits a string on {{PH-nn}} and styles the placeholders. */
function runs(text, base = {}, dark = false) {
  return text
    .split(/(\{\{PH-[0-9–-]+\}\})/)
    .filter(Boolean)
    .map((part) =>
      part.startsWith('{{PH')
        ? { text: part, options: { ...base, bold: true, color: dark ? PALETTE.phFill : PALETTE.phInk, highlight: dark ? undefined : PALETTE.phFill } }
        : { text: part, options: { ...base } },
    );
}

/** Rubric chip: the deck's one repeated motif, top left of every content slide. */
function chip(slide, label) {
  slide.addText(label, {
    shape: pres.shapes.ROUNDED_RECTANGLE,
    rectRadius: 0.12,
    x: M,
    y: 0.25,
    w: Math.max(1.6, 0.075 * label.length + 0.4),
    h: 0.28,
    fontSize: 10,
    bold: true,
    color: PALETTE.navy,
    fill: { color: PALETTE.tint },
    line: { color: PALETTE.navy, width: 0.75 },
    align: 'center',
    valign: 'middle',
    margin: 0,
    isTextBox: true,
    objectName: 'Rubric chip',
  });
}

function content(sectionTitle, rubric, title, claim) {
  const slide = pres.addSlide({ masterName: 'ALIBI_CONTENT', sectionTitle });
  chip(slide, rubric);
  slide.addText(title, { placeholder: 'title' });
  if (claim) {
    slide.addText(runs(claim), {
      x: M,
      y: 1.22,
      w: W - 2 * M,
      h: 0.55,
      fontSize: 14,
      color: PALETTE.soft,
      valign: 'top',
      margin: 0,
      isTextBox: true,
      objectName: 'Claim',
    });
  }
  return slide;
}

/** Bulleted list; each item may contain placeholders. */
function bullets(slide, items, box, size = 14) {
  const paras = items.flatMap((item, i) =>
    runs(item, { fontSize: size, color: PALETTE.ink }).map((r, j, all) => ({
      text: r.text,
      options: {
        ...r.options,
        bullet: j === 0 ? true : undefined,
        indentLevel: 0,
        paraSpaceAfter: j === all.length - 1 ? 6 : 0,
        breakLine: j === all.length - 1 && i < items.length - 1,
      },
    })),
  );
  slide.addText(paras, { ...box, valign: 'top', margin: 0, isTextBox: true });
}

/** A screenshot fitted inside a box, keeping its aspect ratio, with a caption below. */
async function shot(slide, name, box, caption, fallback) {
  if (!existsSync(`assets/${name}.png`) && fallback) [name, caption] = fallback;
  const { width, height } = await sharp(`assets/${name}.png`).metadata();
  const capH = caption ? 0.3 : 0;
  const scale = Math.min(box.w / width, (box.h - capH) / height);
  const w = width * scale;
  const h = height * scale;
  const x = box.x + (box.w - w) / 2;
  slide.addImage({
    path: `assets/${name}.png`,
    x,
    y: box.y,
    w,
    h,
    altText: caption ?? name,
    objectName: `Screenshot ${name}`,
    shadow: { type: 'outer', color: '000000', opacity: 0.18, blur: 6, offset: 2, angle: 90 },
  });
  if (caption) {
    slide.addText(runs(caption), {
      x: box.x,
      y: box.y + h + 0.06,
      w: box.w,
      h: 0.25,
      fontSize: 10,
      italic: true,
      color: PALETTE.faint,
      align: 'center',
      margin: 0,
      isTextBox: true,
    });
  }
}

/** Card with a bold heading and body text. */
function card(slide, { x, y, w, h }, heading, body, opts = {}) {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x,
    y,
    w,
    h,
    rectRadius: 0.08,
    fill: { color: opts.fill ?? PALETTE.tint },
    line: { color: opts.line ?? PALETTE.line, width: 0.75 },
    objectName: `Card ${heading}`,
  });
  slide.addText(
    [
      { text: heading, options: { bold: true, fontSize: opts.headSize ?? 14, color: opts.headColor ?? PALETTE.ink, breakLine: true } },
      ...runs(body, { fontSize: opts.size ?? 12, color: PALETTE.soft }),
    ],
    { x: x + 0.15, y: y + 0.12, w: w - 0.3, h: h - 0.24, valign: 'top', margin: 0, isTextBox: true, paraSpaceAfter: 4 },
  );
}

/** Large number with a small label under it. */
function stat(slide, { x, y, w }, value, label, color = PALETTE.navy) {
  slide.addText(runs(value, { fontSize: 36, bold: true, color }), {
    x,
    y,
    w,
    h: 0.65,
    margin: 0,
    valign: 'bottom',
    isTextBox: true,
  });
  slide.addText(runs(label, { fontSize: 11, color: PALETTE.soft }), { x, y: y + 0.68, w, h: 0.45, margin: 0, valign: 'top', isTextBox: true });
}

const R = {
  traceable: 'Traceable reasoning · 25%',
  testing: 'Testing on unseen data · 25%',
  relevance: 'Relevance and impact · 20%',
  risk: 'Risk controls and oversight · 15%',
  ambition: 'Ambition and technical quality · 15%',
};

// ----------------------------------------------------------------- slides --

// 1. Title
pres.addSection({ title: 'Opening' });
{
  const s = pres.addSlide({ masterName: 'ALIBI_DARK', sectionTitle: 'Opening' });
  s.addText('Alibi', { placeholder: 'title' });
  s.addText(
    [
      { text: 'Your money’s alibi, ready before anyone asks.', options: { breakLine: true, fontSize: 22, color: PALETTE.white } },
      {
        text: 'A financial passport the account holder controls: evidence that explains a bank credit, shared only as far as the question needs.',
        options: { breakLine: true, fontSize: 15 },
      },
      { text: 'AgentVerse 2026 · Finance Track · final commit ', options: { fontSize: 12 } },
      ...runs('{{PH-01}}', { fontSize: 12 }, true),
    ],
    { placeholder: 'body' },
  );
  s.addNotes(
    'Claim: a user-controlled passport that explains a bank credit with checkable evidence and shares only what the holder approves. Supplied by lead (team, links, final commit PH-01). All data is synthetic.',
  );
}

// 2. User
pres.addSection({ title: 'The problem' });
{
  const s = content(
    'The problem',
    `${R.relevance} · User`,
    'Maya has to explain £2,160, and rent is due',
    'A student reselling clothes. Her bank restricts her account until she explains one credit. Her decision: which records to share. The analyst decides separately.',
  );
  stat(s, { x: M, y: 1.9, w: 3.4 }, '£2,160', 'Credit txn-0001-004, booked 28 Oct 2026, reference PO-0001-01');
  bullets(
    s,
    [
      'Bank A asks (rfc-0001-01, 2 Nov): where did it come from, and how is it made up?',
      '£2,400 sales − £180 refunds − £60 fees = £2,160, in integer pence',
      'Synthetic person, bank and marketplace',
    ],
    { x: M, y: 3.25, w: 3.6, h: 1.8 },
  );
  await shot(s, 'bank_case', { x: 4.35, y: 1.95, w: 5.15, h: 2.6 }, 'The bank’s view: Astro bank simulation, case 1 data');
  s.addNotes('Evidence from data, case 1 (agents/data/out/cases/case_0001.json). Screenshot: web/apps/bank.');
}

// 2b. What you can open
{
  const s = content(
    'The problem',
    `${R.ambition} · Product`,
    'Three surfaces, one passport',
    'Maya’s Alibi, the marketplace that feeds it, and the bank that asks: each runs as its own simulated service on the same backend.',
  );
  await shot(s, 'landing', { x: M, y: 1.85, w: 3.0, h: 3.3 }, 'Alibi: www.alibi-app.workers.dev');
  await shot(s, 'phone', { x: 3.55, y: 1.85, w: 2.6, h: 3.3 }, 'Maya’s app');
  await shot(s, 'bank_inbox', { x: 6.35, y: 1.85, w: 3.15, h: 3.3 }, 'Bank A console: bank.alibi-app.workers.dev', ['bank_case', 'Bank A console (case view)']);
  s.addNotes('Live addresses from web/docs/hosting.md. Bank console from agents/execution/web_bank (execution). All data synthetic; banners on every page say so.');
}

// 3. Passport before the request
{
  const s = content(
    'The problem',
    `${R.relevance} · ${R.ambition}`,
    'The evidence was already there',
    'Alibi builds Maya’s history from her merchant and bank connections in the background, and raises an alert before any bank asks.',
  );
  const steps = [
    ['5–14 Oct', '8 sales, 2 refunds, 8 fees pushed by the marketplace'],
    ['26 Oct', 'Release to seller balance, REL-0001-01 (not a bank transfer)'],
    ['28 Oct', 'Bank credit £2,160 booked; Alibi flags it as unexplained'],
    ['2 Nov', 'Bank A’s request arrives; the history is already there'],
  ];
  steps.forEach(([when, what], i) => {
    const y = 1.95 + i * 0.75;
    s.addShape(pres.shapes.OVAL, { x: M, y: y + 0.05, w: 0.3, h: 0.3, fill: { color: PALETTE.navy }, line: { color: PALETTE.navy }, objectName: `Step ${i + 1}` });
    s.addText(String(i + 1), { x: M, y: y + 0.05, w: 0.3, h: 0.3, fontSize: 12, bold: true, color: PALETTE.white, align: 'center', valign: 'middle', margin: 0, isTextBox: true });
    s.addText(
      [
        { text: `${when}  `, options: { bold: true, color: PALETTE.ink, fontSize: 14 } },
        { text: what, options: { color: PALETTE.soft, fontSize: 14 } },
      ],
      { x: M + 0.45, y, w: 5.4, h: 0.65, valign: 'top', margin: 0, isTextBox: true },
    );
  });
  await shot(s, 'phone', { x: 6.6, y: 1.85, w: 2.9, h: 3.35 }, 'Maya’s app, Astro, case 1. Wired alert: {{PH-02}}');
  s.addNotes('Timeline from data case 1. The alert screenshot must come from the wired app with a timestamp before 2 Nov (PH-02).');
}

// 4. Data
pres.addSection({ title: 'How it works' });
{
  const s = content(
    'How it works',
    `${R.testing} · Data`,
    'Synthetic ledgers, written first, with an answer key',
    'Each case is a fictional event ledger. Bank rows, merchant records and reworded requests come from it, with gold answers. Cases 41–50 hide the payout reference so finding it takes interpretation; 71–80 come from a second platform.',
  );
  await shot(s, 'fig1_families', { x: M, y: 1.85, w: 5.3, h: 3.4 }, '80 development cases in 12 families (data, agents/data/figures.py)');
  stat(s, { x: 6.1, y: 1.8, w: 3.4 }, '54 · 12 · 14', 'Gold claim status: supported, contradicted, unresolved');
  stat(s, { x: 6.1, y: 2.95, w: 3.4 }, '{{PH-03}}', 'Sealed cases in {{PH-04}} families, plus {{PH-05}} adversarial; committed only after the freeze');
  bullets(
    s,
    ['Every case hides rent, groceries, pharmacy and a health purchase that must never be shared', '321 checks rebuild the cases and re-derive the gold'],
    { x: 6.1, y: 4.1, w: 3.4, h: 1.0 },
    12,
  );
  s.addNotes('Source: agents/data/out/index.json (80 cases, 12 families) and agents/data/out/figures/fig1_families.png (data). The 10 injection cases repeat cases 1 and 2. Sealed counts PH-03 to PH-05 come from eval’s freeze record.');
}

// 5. How it works
{
  const s = content(
    'How it works',
    `${R.ambition} · ${R.risk}`,
    'Code decides, agents interpret',
    'Deterministic joins run first. Four Gemini roles get separate inputs and return checked proposals. Only the backend does arithmetic, policy, consent, signing and delivery.',
  );
  const boxes = [
    ['Connections', 'Signed merchant webhook and lookup; read-only bank feed'],
    ['Passport', 'Versioned records, links, alerts, sharing history'],
    ['Agents (Gemini)', 'Matcher · Detective · Challenger · Curator'],
    ['Backend checks', 'Arithmetic, policy, consent digest, Ed25519 signing'],
    ['People', 'Maya approves; the bank verifies and an analyst decides'],
  ];
  boxes.forEach(([h, b], i) => {
    const x = M + i * 1.83;
    card(s, { x, y: 2.0, w: 1.65, h: 1.75 }, h, b, { size: 11, headSize: 13, fill: i === 2 ? 'E3ECF7' : PALETTE.tint });
    if (i < boxes.length - 1) {
      s.addShape(pres.shapes.RIGHT_ARROW, { x: x + 1.66, y: 2.75, w: 0.16, h: 0.25, fill: { color: PALETTE.navy }, line: { color: PALETTE.navy }, objectName: 'Flow arrow' });
    }
  });
  s.addText(
    runs('Models: gemini-3.8-flash for routine calls, gemini-4-argon for the frontier role. Which role uses which: {{PH-06}}.', { fontSize: 13, color: PALETTE.ink }),
    { x: M, y: 4.0, w: W - 2 * M, h: 0.4, margin: 0, isTextBox: true },
  );
  s.addText(
    'Agents propose; they never transmit, sign or act on an account. A failed or missing lookup is reported as unresolved, never as a hard-coded success.',
    { x: M, y: 4.45, w: W - 2 * M, h: 0.6, fontSize: 13, color: PALETTE.soft, margin: 0, isTextBox: true },
  );
  s.addNotes('Source: SPEC Architecture and Agent boundaries; model IDs from docs/TASK.md. Role-to-model mapping (PH-06) from alibi/llm/config.py.');
}

// 6. Trace end to end
pres.addSection({ title: 'Traceable reasoning' });
{
  const s = content(
    'Traceable reasoning',
    R.traceable,
    'Click a claim, reach the source',
    'Every claim traces to source IDs and revisions, a scoped lookup, and arithmetic recomputed in code. Case 1, “where did the £2,160 come from?”',
  );
  const steps = [
    'Detective reads the release to balance REL-0001-01. Its message names withdrawal PO-0001-01: not proof of a bank transfer.',
    'Challenger looks up PO-0001-01 and gets payout rec-0001-021: settled 27 Oct 16:00, £2,160, to the bank.',
    'Code checks the reference matches, settled ≤ booked ≤ settled + 5 days, and recomputes £2,160 from 18 records.',
    'Claim supported, citing bank:txn-0001-004 and merchant:rec-0001-021.',
  ];
  steps.forEach((t, i) => {
    const y = 1.9 + i * 0.8;
    s.addShape(pres.shapes.OVAL, { x: M, y: y + 0.02, w: 0.3, h: 0.3, fill: { color: PALETTE.navy }, line: { color: PALETTE.navy }, objectName: `Trace step ${i + 1}` });
    s.addText(String(i + 1), { x: M, y: y + 0.02, w: 0.3, h: 0.3, fontSize: 12, bold: true, color: PALETTE.white, align: 'center', valign: 'middle', margin: 0, isTextBox: true });
    s.addText(t, { x: M + 0.45, y, w: 4.75, h: 0.75, fontSize: 13, color: PALETTE.ink, valign: 'top', margin: 0, isTextBox: true });
  });
  await shot(s, 'trace', { x: 5.85, y: 1.85, w: 3.65, h: 3.35 }, 'Template preview, sample data. Final: case 1 {{PH-07}}');
  s.addNotes('Records and gold from data case 1. Agent trace from research’s development run. Replace the preview with the wired app’s trace on case 1 (PH-07); bank-facing, so no model inputs or private records.');
}

// 7. New record changes the conclusion
{
  const s = content(
    'Traceable reasoning',
    R.traceable,
    'Same amounts, different answer',
    'When the retrieved record says the money had not reached the bank, the explanation is downgraded, even though the amounts still add up.',
  );
  card(
    s,
    { x: M, y: 1.95, w: 4.4, h: 2.0 },
    'Case 1: payout settled before the credit',
    'Same £2,160 and the same release message. Lookup: PO-0001-01 settled 27 Oct 16:00, before the credit was booked on 28 Oct.\nResult: supported.',
    { headColor: PALETTE.ok, size: 13 },
  );
  card(
    s,
    { x: 5.1, y: 1.95, w: 4.4, h: 2.0 },
    'Case 2: payout still pending after the credit',
    'Same £2,160 and the same release message. Lookup: PO-0002-01 still pending on 29 Oct, after the credit was booked on 28 Oct.\nResult: Maya’s claim contradicted; origin unresolved (settlement_pending_at_booking).',
    { headColor: PALETTE.bad, size: 13 },
  );
  s.addText(
    runs('Code-only regression, not an LLM result: case 25, a signed correction to “failed” invalidates the claim and the pending draft {{PH-08}}.', { fontSize: 13, color: PALETTE.soft }),
    { x: M, y: 4.2, w: W - 2 * M, h: 0.55, margin: 0, isTextBox: true },
  );
  s.addNotes('Cases 1, 2 and 25 from data. Case 2 dev trace from research. Invalidation outcome from execution’s probe SP-06 (PH-08).');
}

// 8. Disclosure decision
pres.addSection({ title: 'Impact and controls' });
{
  const s = content(
    'Impact and controls',
    `${R.relevance} · ${R.risk}`,
    'Maya sees exactly what the bank will see',
    'Code builds the smallest package that answers the question. Maya sees what is included, left out and generalised, and approves it for one named bank.',
  );
  card(s, { x: M, y: 1.9, w: 4.9, h: 1.35 }, 'Included', 'The credit, the payout record, and the 18 sale, refund and fee records it covers.', { headColor: PALETTE.ok, size: 13 });
  card(
    s,
    { x: M, y: 3.4, w: 4.9, h: 1.65 },
    'Left out, privately listed',
    'Rent txn-0001-001, two card payments, and a prescription-eyewear purchase rec-0001-019. One plain-English rule, compiled and confirmed by Maya.',
    { headColor: PALETTE.bad, size: 13 },
  );
  await shot(s, 'review', { x: 5.65, y: 1.85, w: 3.85, h: 3.35 }, 'Template preview, sample data. Final: case 1 {{PH-09}}');
  s.addNotes('Included and forbidden sets from data case 1 gold. Policy compilation from execution. Screenshot PH-09 from the wired app.');
}

// 9. Live refusal
{
  const s = content(
    'Impact and controls',
    `${R.risk} · Controls`,
    'The backend says no, live',
    'Editing an approved package, reaching another bank’s inbox, or hiding instructions in a receipt is refused in code, and every refusal is logged.',
  );
  const probes = [
    ['SP-02 Edited after approval', 'Refused: approval_digest_mismatch. A fresh approval of the new body then dispatches.'],
    ['SP-17, SP-18 Bank A reaches for Bank B', 'Listing or fetching Bank B’s package is refused, without confirming it exists.'],
    ['SP-38 Instructions hidden in a receipt', 'No out-of-scope read or disclosure; the refusal shows in the trace and audit log.'],
  ];
  probes.push(['SP-25 Six bytes changed in a signed package', 'Bank verifier: body_sha256 mismatch and Alibi signature invalid; rejected (tamper demo).']);
  probes.forEach(([h, b], i) => card(s, { x: M, y: 1.85 + i * 0.7, w: 4.9, h: 0.64 }, h, b, { size: 10, headSize: 11 }));
  s.addText(
    runs('Sealed run: {{PH-10}} of {{PH-11}} attack probes blocked; {{PH-12}} valid controls allowed. Bank-verifier reference run (not the live service): 8 of 8 probes pass.', { fontSize: 11, color: PALETTE.ink, bold: true }),
    { x: M, y: 4.7, w: 4.9, h: 0.6, margin: 0, isTextBox: true },
  );
  await shot(s, 'refused', { x: 5.65, y: 1.85, w: 3.85, h: 3.35 }, 'Template preview of the refusal, sample data');
  s.addNotes('Probes from agents/execution/out/PROBES.md. Totals PH-10 to PH-12 from eval via agents.execution.api.summarise. Reference run: agents/execution/out/results.jsonl record fdd4570242 (split reference), reproduced independently by eval; it tests the reference verifier, not the live bank service. Tamper demo: agents/execution/out/TAMPER_DEMO.md.');
}

// 10. Oversight
{
  const s = content(
    'Impact and controls',
    `${R.risk} · Controls`,
    'Maya approves, the analyst decides, nothing is automatic',
    'Three separate checks: is the package genuine, who vouches for each source, and what does the evidence support. A valid signature never means “all true”.',
  );
  bullets(
    s,
    [
      'Approval is bound to the SHA-256 of the body, the audience, the request ID, the policy version and the source revisions',
      'The analyst records clear, escalate or request more; no account is cleared automatically',
      'A self-supplied CSV (case 39) is reviewable at lower trust, never issuer-verified',
      'Every approve, dispatch, receipt and decision leaves an audit entry',
    ],
    { x: M, y: 1.9, w: 4.9, h: 3.2 },
    13,
  );
  await shot(s, 'bank_package', { x: 5.65, y: 1.85, w: 3.85, h: 3.35 }, 'Bank A console, case 1 package: three separate checks', ['analyst_checks', 'Template preview, sample data. Final: {{PH-13}}']);
  s.addNotes('Source: agents/execution/out/ANALYST_FLOW.md and the Disposition row in shared/CONTRACTS.md. Screenshot PH-13 from the wired analyst console.');
}

// 11. Reuse
{
  const s = content(
    'Impact and controls',
    R.relevance,
    'One passport, a different question, a new approval',
    'Bank B asks something else. The same stored history answers it with no new import, its own package and its own approval; neither bank sees the other’s.',
  );
  card(s, { x: M, y: 1.95, w: 4.4, h: 1.35 }, 'Bank A · rfc-0001-01', 'Source and breakdown of the £2,160 credit.\nCites the credit, the payout and 18 covered records.', { size: 13 });
  card(s, { x: 5.1, y: 1.95, w: 4.4, h: 1.35 }, 'Bank B · rfc-0001-02', 'Sales and refunds in October 2026.\n8 sales £2,400 and 2 refunds £180, from 10 reused source IDs.', { size: 13 });
  s.addText(
    runs('Portability checks {{PH-14}}: same store, reused IDs, distinct packages, isolated inboxes. Reusing Bank A’s approval for Bank B is refused (SP-03).', { fontSize: 13, color: PALETTE.ink }),
    { x: M, y: 3.55, w: W - 2 * M, h: 0.9, margin: 0, isTextBox: true },
  );
  s.addNotes('Bank B gold from data case 1. Portability report from eval (PH-14). SP-03 and SP-17 from execution.');
}

// 12. Benchmark
pres.addSection({ title: 'Testing on unseen data' });
{
  const s = content(
    'Testing on unseen data',
    `${R.testing} · Benchmark`,
    'Three systems, same inputs, sealed cases',
    'Full Alibi against a rules baseline and a one-pass Gemini baseline, on cases nobody building the app had seen. The baselines are not weakened.',
  );
  const sys = [
    ['Rules', 'Reference, amount and date matching, reconciliation, fixed evidence selection'],
    ['One-pass Gemini', 'One call drafts the whole package, with a fixed retrieval strategy'],
    ['Full Alibi', 'Four roles with adaptive retrieval and a challenge step'],
  ];
  sys.forEach(([h, b], i) => card(s, { x: M + i * 3.05, y: 1.95, w: 2.9, h: 1.5 }, h, b, { size: 12, fill: i === 2 ? 'E3ECF7' : PALETTE.tint }));
  bullets(
    s,
    [
      'Same input, candidate pool, validation and policy gate for all three',
      'Primary endpoint: safe, correct case resolution',
      'Frozen candidate {{PH-15}}, commitments to inputs and gold {{PH-16}}, reruns disclosed {{PH-17}}',
    ],
    { x: M, y: 3.65, w: W - 2 * M, h: 1.4 },
    13,
  );
  s.addNotes('Source: SPEC Evaluation. Freeze record PH-15 to PH-17 from eval.');
}

// 13. Results
{
  const s = content('Testing on unseen data', `${R.testing} · Results`, 'Held-out results', '{{PH-18}}');
  const head = ['Metric', 'Rules', 'One-pass Gemini', 'Alibi'].map((t) => ({
    text: t,
    options: { bold: true, color: PALETTE.white, fill: { color: PALETTE.navy } },
  }));
  const rows = [
    ['Safe and complete / cases', '{{PH-20}}', '{{PH-21}}', '{{PH-22}}'],
    ['Match precision / recall (unmatched)', '{{PH-23}}', '{{PH-24}}', '{{PH-25}}'],
    ['Citation accuracy', '{{PH-26}}', '{{PH-27}}', '{{PH-28}}'],
    ['Gap recall', '{{PH-29}}', '{{PH-30}}', '{{PH-31}}'],
    ['Forbidden / unnecessary items', '{{PH-32}}', '{{PH-33}}', '{{PH-34}}'],
    ['Unsafe proposals before the policy gate', '{{PH-35}}', '{{PH-36}}', '{{PH-37}}'],
  ].map((r) =>
    r.map((c, i) =>
      i === 0
        ? { text: c, options: { color: PALETTE.ink } }
        : { text: c, options: { bold: true, color: PALETTE.phInk, fill: { color: PALETTE.phFill }, align: 'center' } },
    ),
  );
  s.addTable([head, ...rows], {
    x: M,
    y: 1.9,
    w: W - 2 * M,
    colW: [3.9, 1.7, 1.7, 1.7],
    fontSize: 13,
    fontFace: FONT.body,
    border: { type: 'solid', pt: 0.75, color: PALETTE.line },
    rowH: 0.42,
    valign: 'middle',
    margin: 0.06,
  });
  s.addText('Failures and abstentions count in every denominator. Same keys for all three systems.', {
    x: M,
    y: 4.95,
    w: W - 2 * M,
    h: 0.25,
    fontSize: 11,
    color: PALETTE.faint,
    margin: 0,
    isTextBox: true,
  });
  s.addNotes('Every cell from eval’s verified lockbox records (results.py verify). Write the headline PH-18 only after the numbers land, including if Alibi does not win.');
}

// 14. Uncertainty, cost, failure
{
  const s = content(
    'Testing on unseen data',
    R.testing,
    'How sure we are, what it cost, where it broke',
    'Sample size is stated as cases and as independent families. One case Alibi got wrong, with its trace.',
  );
  card(s, { x: M, y: 1.95, w: 2.9, h: 2.1 }, 'Uncertainty', '95% intervals by case-family resampling, or paired win/tie/loss counts if there are too few families.\n{{PH-38}}', { size: 13 });
  card(s, { x: M + 3.05, y: 1.95, w: 2.9, h: 2.1 }, 'Cost', 'Per case: latency, model calls, tool calls, tokens and cost, each marked observed or estimated.\n{{PH-39}}', { size: 13 });
  card(s, { x: M + 6.1, y: 1.95, w: 2.9, h: 2.1 }, 'A failure', 'One representative failure: what went wrong, the trace, and the cause.\n{{PH-40}}', { size: 13, headColor: PALETTE.bad });
  s.addNotes('Intervals and cost from eval (PH-38, PH-39). Failure case from eval, trace and cause from research (PH-40).');
}

// 15. What could fool us
{
  const s = content(
    'Testing on unseen data',
    R.testing,
    'What could fool us, and what we did about it',
    'The obvious ways this evaluation could flatter Alibi, each with its guard.',
  );
  const cell = (t, o = {}) => ({ text: runs(t, { fontSize: 10, color: PALETTE.ink, ...o }), options: { valign: 'top' } });
  const head = ['Threat', 'Guard'].map((t) => ({
    text: t,
    options: { bold: true, color: PALETTE.white, fill: { color: PALETTE.navy } },
  }));
  const rows = [
    ['Gold leaks into the system under test', 'Public and private views checked against independently parsed times; the subprocess is denied the gold path. Rechecked on the frozen adapter.'],
    ['Generator and rules overlap, so a baseline wins by design', 'Sealed cases written separately by eval: {{PH-03}} cases in {{PH-04}} families, plus {{PH-05}} adversarial. The rules baseline scored below the ceiling on them.'],
    ['Variants of one scenario are not independent', 'Families counted apart from variants; intervals by 2,000 family resamples or paired win/tie/loss {{PH-38}}.'],
    ['Injection carriers prove nothing', 'A probe needs a landed marker, a structured refusal and an audit entry; an allowed read is the negative control.'],
    ['One system reuses another’s cache', 'Separate workers and scratch stores per system; one-pass Gemini runs with its cache off.'],
    ['Reruns are cherry-picked', 'A manifest binds one capture; predictions are hashed and the scorer replays them. Reruns disclosed {{PH-17}}.'],
  ].map(([a, b]) => [cell(a, { color: PALETTE.soft }), cell(b)]);
  s.addTable([head, ...rows], {
    x: M,
    y: 1.7,
    w: W - 2 * M,
    colW: [3.2, 5.8],
    fontSize: 10,
    fontFace: FONT.body,
    border: { type: 'solid', pt: 0.75, color: PALETTE.line },
    margin: 0.07,
  });
  s.addNotes('Six rows from agents/eval/out/METHOD.md “What could fool us” (eval), condensed. Counts and commitments PH-03 to PH-05, PH-17 and PH-38 from eval’s freeze record.');
}

// 16. Adaptive retrieval
{
  const s = content(
    'Testing on unseen data',
    R.ambition,
    'Choosing the next lookup is where the model earns its place',
    '{{PH-41}}',
  );
  stat(s, { x: M, y: 1.85, w: 2.6 }, '{{PH-43}}', 'Safe and complete on lookup-dependent sealed cases, one-pass vs Alibi');
  stat(s, { x: M + 2.9, y: 1.85, w: 2.6 }, '{{PH-44}}', 'Lookups and records read per system');
  card(
    s,
    { x: M, y: 3.25, w: 5.4, h: 1.75 },
    'Not credited to the model',
    'Detecting status “pending”, parsing exact references and all arithmetic are done in code, and in the rules baseline too.',
    { size: 13 },
  );
  card(
    s,
    { x: 6.15, y: 1.95, w: 3.35, h: 3.05 },
    'Live tool selection',
    'The Challenger picks the payout lookup by reference from free text, then revises or confirms the claim.\n{{PH-42}}',
    { size: 13, fill: 'E3ECF7' },
  );
  s.addNotes('PH-43 and PH-44 from eval (split on requires_retrieval; keys safe_complete_rate, lookups, observed_inputs). PH-42 is a screenshot or log line from research’s live run. If the gap is zero or negative, say so.');
}

// 17. Limits
pres.addSection({ title: 'Close' });
{
  const s = content('Close', 'Limits', 'What this does not show', 'The results measure a simulated task on synthetic records.');
  card(
    s,
    { x: M, y: 1.95, w: 4.4, h: 2.3 },
    'Out of scope',
    'Real bank acceptance and fewer account restrictions are unmeasured.\nBoth connections are simulated services, not live integrations.\nMonitoring covers observable activity, not a bank’s anti-money-laundering system.',
    { size: 13 },
  );
  card(
    s,
    { x: 5.1, y: 1.95, w: 4.4, h: 2.3 },
    'Trust boundaries',
    'The app holds the encryption key; evidence is encrypted at rest only when that key is kept outside the data folder (SP-47). Gemini can read the synthetic data sent to it.\nNot a W3C Verifiable Credential.\nUntested or replayed features: {{PH-45}}',
    { size: 13 },
  );
  s.addNotes('Source: SPEC Prototype boundaries. PH-45 from lead’s integration status; eval adds sample-size caveats.');
}

// 18. Close
{
  const s = pres.addSlide({ masterName: 'ALIBI_DARK', sectionTitle: 'Close' });
  s.addText('Evidence that is ready, disclosure that is chosen', { placeholder: 'title', fontSize: 34 });
  s.addText(
    [
      { text: 'Every claim traces to a source and to code-computed arithmetic.', options: { breakLine: true } },
      { text: 'Maya approves each disclosure; the backend refuses everything else.', options: { breakLine: true } },
      { text: 'One history, reused for a second bank with a fresh approval.', options: { breakLine: true } },
      ...runs('Held-out result: {{PH-18}} · Repo and demo: {{PH-01}}', {}, true),
    ],
    { placeholder: 'body' },
  );
  s.addNotes('Recap of slides 6, 9, 11 and 13. Links from lead.');
}

await pres.writeFile({ fileName: OUT });
// Folder entries in the zip break opening and co-editing through cloud services.
spawnSync('zip', ['-dq', OUT, '*/']);
console.log(`wrote ${OUT}`);
