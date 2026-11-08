import { describe, expect, it } from 'vitest';

import { STAGES, agentsLit, packetState, progressOf, stageAt, stageIndex } from '../src/lib/stream';

describe('passport stream timeline', () => {
  it('covers 0..1 with consecutive stages in story order', () => {
    expect(STAGES[0]!.from).toBe(0);
    for (let i = 1; i < STAGES.length; i++) expect(STAGES[i]!.from).toBe(STAGES[i - 1]!.to);
    expect(STAGES.map((s) => s.id)).toEqual([
      'intro',
      'merchants',
      'banks',
      'engine',
      'request',
      'approve',
      'deliver',
    ]);
  });

  it('picks the stage for any progress, clamping out-of-range scroll', () => {
    expect(stageAt(-1).id).toBe('intro');
    expect(stageAt(0.2).id).toBe('merchants');
    expect(stageAt(0.65).id).toBe('request');
    expect(stageAt(1).id).toBe('deliver');
    expect(stageAt(2).id).toBe('deliver');
    expect(stageIndex(0.5)).toBe(3);
  });

  it('maps section scroll to progress', () => {
    expect(progressOf(0, 5000, 1000)).toBe(0);
    expect(progressOf(-2000, 5000, 1000)).toBe(0.5);
    expect(progressOf(-9000, 5000, 1000)).toBe(1);
    expect(progressOf(300, 5000, 1000)).toBe(0);
  });

  it('moves packets only inside their window, holding the final package at the bank', () => {
    const w = { from: 0.1, to: 0.2 };
    expect(packetState(0.05, w)).toEqual({ t: 0, visible: false });
    expect(packetState(0.15, w).visible).toBe(true);
    expect(packetState(0.15, w).t).toBeCloseTo(0.5);
    expect(packetState(0.25, w)).toEqual({ t: 1, visible: false });
    expect(packetState(0.95, { from: 0.88, to: 0.97, hold: true })).toMatchObject({
      visible: true,
    });
    expect(packetState(1, { from: 0.88, to: 0.97, hold: true })).toEqual({ t: 1, visible: true });
  });

  it('lights the agents one after another during the engine stage', () => {
    expect(agentsLit(0.4)).toBe(0);
    expect(agentsLit(0.47)).toBe(1);
    expect(agentsLit(0.6)).toBe(4);
    expect(agentsLit(0.9)).toBe(4);
  });
});
