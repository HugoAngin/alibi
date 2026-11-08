/**
 * The public case 1 preview (`/preview/app/...`) reuses the app's own screens, whose links point
 * into `/app/`. Inside the preview, links to Home and to a request stay in the preview; every other
 * app link (Account, merchants) keeps its `/app/` address, which asks the visitor to sign in.
 */

export const PREVIEW_PREFIX = '/preview/app';

export function isPreviewRoute(pathname: string): boolean {
  return pathname === PREVIEW_PREFIX || pathname.startsWith(`${PREVIEW_PREFIX}/`);
}

/** Rewrites `href="/app/"` and `href="/app/requests/<id>/..."` to their preview pages. */
export function previewLinks(html: string): string {
  return html.replace(
    /href="\/app\/((?:requests\/[^"/#?]+\/)?(?:#[^"]*)?)"/g,
    (_whole, rest: string) => `href="${PREVIEW_PREFIX}/${rest}"`,
  );
}
