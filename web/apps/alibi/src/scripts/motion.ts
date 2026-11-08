/**
 * Site motion: Lenis smooth scrolling, smooth in-page anchors, and
 * fade-up reveals for elements marked
 * `data-reveal`. Everything is skipped when the visitor prefers reduced
 * motion. Other scripts can follow the scroll through the `alibi:scroll`
 * window event, which carries the scroll position.
 */
import Lenis from 'lenis';

const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

if (!reduced) {
  const lenis = new Lenis({ autoRaf: true, lerp: 0.1 });
  lenis.on('scroll', ({ scroll }: { scroll: number }) => {
    window.dispatchEvent(new CustomEvent('alibi:scroll', { detail: { scroll } }));
  });

  // Same-page anchors scroll with Lenis instead of jumping.
  document.addEventListener('click', (event) => {
    const link = (event.target as Element).closest<HTMLAnchorElement>('a[href*="#"]');
    if (!link) return;
    const url = new URL(link.href);
    if (url.pathname !== window.location.pathname || !url.hash) return;
    const target = document.querySelector(url.hash);
    if (!target) return;
    event.preventDefault();
    lenis.scrollTo(target as HTMLElement, { offset: -24 });
    history.replaceState(null, '', url.hash);
  });
} else {
  window.addEventListener('scroll', () => {
    window.dispatchEvent(new CustomEvent('alibi:scroll', { detail: { scroll: window.scrollY } }));
  });
}

const revealed = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-in');
        revealed.unobserve(entry.target);
      }
    }
  },
  { rootMargin: '0px 0px -10% 0px', threshold: 0.12 },
);
document.querySelectorAll('[data-reveal]').forEach((el) => revealed.observe(el));
