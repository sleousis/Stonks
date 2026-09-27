import { DestroyRef, type Signal, effect, inject, signal, untracked } from '@angular/core';

/** Default length of a count-up; matches the `--dur-count` token. */
export const COUNT_UP_MS = 700;

/** True when the browser can animate and the person has not asked for less motion. */
export function motionAllowed(): boolean {
  const media = globalThis.matchMedia;
  if (typeof media !== 'function' || typeof globalThis.requestAnimationFrame !== 'function') {
    return false;
  }
  return !media.call(globalThis, '(prefers-reduced-motion: reduce)').matches;
}

/** Ease-out cubic: fast start, gentle landing. */
export function easeOut(t: number): number {
  const c = Math.min(1, Math.max(0, t));
  return 1 - (1 - c) ** 3;
}

/**
 * A number that counts up to its source: from 0 the first time, then from
 * the last value to the new one. With reduced motion (or no animation
 * frames, as in tests) it jumps straight to the value. Call in an injection
 * context.
 *
 *   protected readonly shown = countUp(() => this.total());
 */
export function countUp(
  source: () => number | null,
  durationMs = COUNT_UP_MS,
): Signal<number | null> {
  const out = signal<number | null>(null);
  let frame = 0;
  const cancel = () => {
    if (frame) globalThis.cancelAnimationFrame?.(frame);
    frame = 0;
  };
  inject(DestroyRef).onDestroy(cancel);

  effect(() => {
    const target = source();
    untracked(() => {
      cancel();
      if (target === null || !Number.isFinite(target) || !motionAllowed() || durationMs <= 0) {
        out.set(target);
        return;
      }
      const from = out() ?? 0;
      if (from === target) {
        out.set(target);
        return;
      }
      const start = performance.now();
      const step = (now: number) => {
        const t = (now - start) / durationMs;
        if (t >= 1) {
          out.set(target);
          frame = 0;
          return;
        }
        out.set(from + (target - from) * easeOut(t));
        frame = requestAnimationFrame(step);
      };
      frame = requestAnimationFrame(step);
    });
  });
  return out.asReadonly();
}
