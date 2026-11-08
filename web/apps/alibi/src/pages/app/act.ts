/**
 * POST /app/act: one of Maya's actions (run investigation, remove an item, approve, send),
 * forwarded to the backend by src/lib/holderAct.ts; she lands back on /app/ screens.
 */
import type { APIRoute } from 'astro';

import { holderAct } from '@/lib/holderAct';

export const prerender = false;

export const POST: APIRoute = (context) => holderAct(context, '/app');
