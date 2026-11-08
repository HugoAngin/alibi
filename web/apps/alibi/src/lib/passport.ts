/**
 * Loads Maya's passport home: live from the backend when it answers with the
 * screen's JSON, otherwise the built-in case 1 data (and the reason why).
 */

import { ALIBI_API_ORIGIN } from 'astro:env/server';

import { passportFixture } from '@/data/fixtures';
import { loadContext } from '@alibi/ui/api';

import { isPassportHome } from './api/guards';
import { ROUTES, backendHeaders } from './screens';

export function loadPassport() {
  return loadContext({
    origin: ALIBI_API_ORIGIN,
    path: ROUTES.passport,
    isShape: isPassportHome,
    fixture: passportFixture,
    headers: backendHeaders(),
  });
}
