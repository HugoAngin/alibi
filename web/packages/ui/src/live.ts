/**
 * Browser side of the live demo. Polls the site's own `/live/` endpoint (same origin; the demo
 * credential stays on the server) and reloads the page when the demo's state changes, keeping the
 * scroll position. A page with a half-filled form is not reloaded under the user: a small notice
 * offers the refresh instead. Polling pauses while the tab is hidden and checks again on return.
 * If the endpoint says the demo is off (`version: null`), polling stops.
 */

const SCROLL_KEY = 'alibi-live-scroll';

export function startLive(opts: { endpoint?: string; intervalMs?: number } = {}): void {
  const endpoint = opts.endpoint ?? '/live/';
  const interval = opts.intervalMs ?? 1500;
  let known: string | null | undefined;
  let dirty = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;

  try {
    const saved = sessionStorage.getItem(SCROLL_KEY);
    if (saved) {
      sessionStorage.removeItem(SCROLL_KEY);
      const { path, y } = JSON.parse(saved) as { path: string; y: number };
      if (path === location.pathname) window.scrollTo(0, y);
    }
  } catch {
    // storage blocked: reloads simply start at the top
  }

  document.addEventListener('input', (e) => {
    const t = e.target as HTMLElement | null;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) dirty = true;
  });
  document.addEventListener('submit', (e) => {
    // The page is navigating on its own, unless the submit was cancelled (a declined confirm).
    if (!e.defaultPrevented) stopped = true;
  });

  function notice(): void {
    if (document.getElementById('live-notice')) return;
    const box = document.createElement('div');
    box.id = 'live-notice';
    box.setAttribute('role', 'status');
    box.style.cssText =
      'position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:9999;' +
      'padding:10px 14px;border-radius:999px;background:#111;color:#fff;font:600 14px/1.2 system-ui,sans-serif;' +
      'box-shadow:0 6px 24px rgba(0,0,0,.25);display:flex;gap:10px;align-items:center';
    box.textContent = 'New activity.';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Refresh';
    btn.style.cssText =
      'border:0;border-radius:999px;padding:6px 12px;font:inherit;cursor:pointer;background:#fff;color:#111';
    btn.addEventListener('click', reload);
    box.append(btn);
    document.body.append(box);
  }

  function reload(): void {
    try {
      sessionStorage.setItem(
        SCROLL_KEY,
        JSON.stringify({ path: location.pathname, y: window.scrollY }),
      );
    } catch {
      // ignore
    }
    location.reload();
  }

  async function check(): Promise<void> {
    if (stopped) return;
    try {
      const r = await fetch(endpoint, {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (r.ok) {
        const { version } = (await r.json()) as { version: string | null };
        // A form was submitted while this poll was in flight: the page is navigating on its own,
        // and reloading now would cancel that post.
        if (stopped) return;
        if (version === null) {
          stopped = true;
          return;
        }
        if (known === undefined) known = version;
        else if (version !== known) {
          known = version;
          if (dirty) notice();
          else return reload();
        }
      }
    } catch {
      // a missed poll is fine; the next one catches up
    }
    schedule();
  }

  function schedule(): void {
    clearTimeout(timer);
    if (!stopped && !document.hidden) timer = setTimeout(check, interval);
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) void check();
    else clearTimeout(timer);
  });
  void check();
}
