/**
 * `/simulate/` is Maya's dashboard shown to any visitor: the app's own screens, live from the
 * backend, with every action disabled. Those screens link into `/app/`; inside the simulation, links
 * to Home, a request, Account and the passport page stay in the simulation, and every other app
 * link (merchants, profile, exports) keeps its `/app/` address, which asks the visitor to sign in
 * to their own dashboard.
 */

export const SIMULATION_PREFIX = '/simulate';

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
