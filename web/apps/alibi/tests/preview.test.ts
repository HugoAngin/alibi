/**
 * The public case 1 preview keeps Home and request links inside the preview, and leaves every other
 * app link (Account, merchants, actions) on `/app/`, which needs sign-in.
 */
import { describe, expect, it } from 'vitest';

import { isPreviewRoute, previewLinks } from '../src/lib/preview';

describe('previewLinks', () => {
  it('keeps Home and request links in the preview, anchors included', () => {
    expect(previewLinks('<a href="/app/">Home</a>')).toBe('<a href="/preview/app/">Home</a>');
    expect(previewLinks('<a href="/app/requests/rfc-0001-01/">x</a>')).toBe(
      '<a href="/preview/app/requests/rfc-0001-01/">x</a>',
    );
    expect(previewLinks('<a href="/app/requests/rfc-0001-01/#step-sof">x</a>')).toBe(
      '<a href="/preview/app/requests/rfc-0001-01/#step-sof">x</a>',
    );
  });

  it('leaves Account, merchants, exports and form actions alone', () => {
    const html =
      '<a href="/app/account/"></a><a href="/app/merchants/"></a>' +
      '<a href="/app/requests/rfc-0001-01/export.json"></a><form action="/app/act"></form>';
    expect(previewLinks(html)).toBe(html);
  });
});

describe('isPreviewRoute', () => {
  it('matches only the preview tree', () => {
    expect(isPreviewRoute('/preview/app/')).toBe(true);
    expect(isPreviewRoute('/preview/app/requests/rfc-0001-01/')).toBe(true);
    expect(isPreviewRoute('/app/')).toBe(false);
    expect(isPreviewRoute('/preview/apple')).toBe(false);
  });
});
