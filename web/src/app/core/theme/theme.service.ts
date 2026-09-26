import { DOCUMENT } from '@angular/common';
import { Injectable, computed, effect, inject, signal } from '@angular/core';

export type ThemeMode = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

const KEY = 'stonks.theme';

/**
 * Light/dark theme. `system` follows prefers-color-scheme; `light`/`dark`
 * force it via `html[data-theme]`. The choice is a per-browser convenience
 * kept in localStorage (it is not sensitive).
 */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  private readonly doc = inject(DOCUMENT);
  private readonly media = this.doc.defaultView?.matchMedia?.('(prefers-color-scheme: dark)');
  private readonly systemDark = signal(this.media?.matches ?? false);

  readonly mode = signal<ThemeMode>(readMode());
  readonly resolved = computed<ResolvedTheme>(() => {
    const mode = this.mode();
    if (mode === 'system') return this.systemDark() ? 'dark' : 'light';
    return mode;
  });

  constructor() {
    this.media?.addEventListener?.('change', (e) => this.systemDark.set(e.matches));
    effect(() => {
      const mode = this.mode();
      const root = this.doc.documentElement;
      if (mode === 'system') delete root.dataset['theme'];
      else root.dataset['theme'] = mode;
      try {
        if (mode === 'system') localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, mode);
      } catch {
        // Storage blocked: the theme still applies for this page view.
      }
    });
  }

  setMode(mode: ThemeMode): void {
    this.mode.set(mode);
  }

  /** Flip between light and dark (leaving "system"). */
  toggle(): void {
    this.mode.set(this.resolved() === 'dark' ? 'light' : 'dark');
  }
}

function readMode(): ThemeMode {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    return 'system';
  }
}
