/**
 * Whether Maya's simulation dashboard (`/simulate/`) can act: run, approve, send and reset. True on
 * a simulated site (the visitor acts in their own run of the recorded demo) and in the local live
 * demo (ALIBI_SIMULATE_ACTIONS=1, set by `pnpm demo`); read-only otherwise.
 */
import { ALIBI_SIMULATE_ACTIONS } from 'astro:env/server';

import { isSimulated } from '@alibi/ui/sim/context';

export function simulationActs(): boolean {
  return isSimulated() || ALIBI_SIMULATE_ACTIONS === '1';
}
