/**
 * `/simulate/` is Maya's dashboard shown to any visitor: the app's own screens, live from the
 * backend, with every action disabled. Those screens link into `/app/`; inside the simulation, links
 * to Home and to a request stay in the simulation, and every other app link (Account, merchants,
 * exports) keeps its `/app/` address, which asks the visitor to sign in to their own dashboard.
 */

export const SIMULATION_PREFIX = '/simulate';

export function isSimulationRoute(pathname: string): boolean {
  return pathname === SIMULATION_PREFIX || pathname.startsWith(`${SIMULATION_PREFIX}/`);
}

/** Rewrites `href="/app/"` and `href="/app/requests/<id>/..."` to their simulation pages. */
export function simulationLinks(html: string): string {
  return html.replace(
    /href="\/app\/((?:requests\/[^"/#?]+\/)?(?:#[^"]*)?)"/g,
    (_whole, rest: string) => `href="${SIMULATION_PREFIX}/${rest}"`,
  );
}
