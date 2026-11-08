/**
 * The landing page's passport data stream, as a scroll timeline. Scroll progress `p` (0 at the
 * top of the stream section, 1 at its end) picks the stage and places every packet along its
 * path, so the whole story is scrubbed by scroll rather than played on a clock. Pure, so it is
 * unit tested; src/components/site/PassportStream.astro draws it.
 */

export type StageId =
  'intro' | 'merchants' | 'banks' | 'engine' | 'request' | 'approve' | 'deliver';

export interface Stage {
  id: StageId;
  from: number;
  to: number;
  /** A few words shown while the stage is active. */
  caption: string;
}

export const STAGES: Stage[] = [
  { id: 'intro', from: 0, to: 0.08, caption: '' },
  { id: 'merchants', from: 0.08, to: 0.3, caption: 'Merchants connect' },
  { id: 'banks', from: 0.3, to: 0.46, caption: 'Your banks stream in' },
  { id: 'engine', from: 0.46, to: 0.62, caption: 'Agents investigate. Code decides.' },
  { id: 'request', from: 0.62, to: 0.72, caption: 'A bank asks' },
  { id: 'approve', from: 0.72, to: 0.86, caption: 'You choose what leaves' },
  { id: 'deliver', from: 0.86, to: 1.0001, caption: 'A signed answer, delivered' },
];

export function clamp01(x: number): number {
  return x < 0 ? 0 : x > 1 ? 1 : x;
}

export function stageAt(p: number): Stage {
  const q = clamp01(p);
  return STAGES.find((s) => q >= s.from && q < s.to) ?? STAGES[STAGES.length - 1]!;
}

/** Index of the stage, 0..6; drives `data-stage` and everything that switches on once. */
export function stageIndex(p: number): number {
  return STAGES.indexOf(stageAt(p));
}

/** Scroll progress through a section whose sticky stage is one viewport tall. */
export function progressOf(sectionTop: number, sectionHeight: number, viewport: number): number {
  const travel = sectionHeight - viewport;
  return travel <= 0 ? 1 : clamp01(-sectionTop / travel) + 0;
}

export interface PacketTiming {
  /** Progress at which the packet leaves its source. */
  from: number;
  /** Progress at which it arrives. */
  to: number;
  /** Stay visible at the destination after arriving (the final package). */
  hold?: boolean;
}

/** Where a packet is along its path (0..1) and whether it is drawn. */
export function packetState(p: number, t: PacketTiming): { t: number; visible: boolean } {
  if (p < t.from) return { t: 0, visible: false };
  const span = t.to - t.from;
  const along = span <= 0 ? 1 : clamp01((p - t.from) / span);
  if (along >= 1) return { t: 1, visible: Boolean(t.hold) };
  return { t: easeInOut(along), visible: true };
}

/** Smooth start and stop for packets: the site's easing in spirit, symmetric so scrubbing back feels the same. */
export function easeInOut(x: number): number {
  const t = clamp01(x);
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** Which agents are lit during the engine stage (one after another, all lit afterwards). */
export function agentsLit(p: number, count = 4): number {
  const engine = STAGES.find((s) => s.id === 'engine')!;
  if (p < engine.from) return 0;
  if (p >= engine.to) return count;
  return Math.min(count, 1 + Math.floor(((p - engine.from) / (engine.to - engine.from)) * count));
}
