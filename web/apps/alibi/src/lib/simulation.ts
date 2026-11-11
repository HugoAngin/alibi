/**
 * `/simulate/` is Maya's dashboard shown to any visitor: the app's own screens. Those screens link
 * into `/app/`; inside the simulation, links to Home, a request, Account and the passport page stay
 * in the simulation, and every other app link (merchants, banks, exports) keeps its `/app/`
 * address.
 *
 * On a simulated site (ALIBI_SIMULATION=1) the visitor acts as Maya against the recorded demo:
 * `/simulate/` takes actions, and the `/app/` pages that have a simulation twin redirect to it
 * (`simulationTwin`), while the others show Maya's simulated data in place.
 */

export const SIMULATION_PREFIX = '/simulate';

/** The `/simulate/` page that shows the same screen as this `/app/` path, if there is one. */
export function simulationTwin(pathname: string): string | null {
  const p = pathname.endsWith('/') ? pathname : `${pathname}/`;
  if (
    p === '/app/' ||
    p === '/app/account/' ||
    p === '/app/account/passport/' ||
    /^\/app\/requests\/[^/]+\/(?:sent\/)?$/.test(p)
  )
    return `${SIMULATION_PREFIX}/${p.slice('/app/'.length)}`;
  return null;
}

export function isSimulationRoute(pathname: string): boolean {
  return pathname === SIMULATION_PREFIX || pathname.startsWith(`${SIMULATION_PREFIX}/`);
}

/**
 * Rewrites `href="/app/"`, `href="/app/requests/<id>/..."`, `href="/app/account/"` and
 * `href="/app/account/passport/"` to their simulation pages.
 */
export function simulationLinks(html: string): string {
  return html.replace(
    /href="\/app\/((?:requests\/[^"/#?]+\/|account\/(?:passport\/)?)?(?:#[^"]*)?)"/g,
    (_whole, rest: string) => `href="${SIMULATION_PREFIX}/${rest}"`,
  );
}
